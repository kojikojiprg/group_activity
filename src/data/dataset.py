import functools
import gc
import itertools
import os
import sys
import time
import warnings
from glob import glob
from multiprocessing import shared_memory
from multiprocessing.managers import SyncManager
from types import SimpleNamespace

warnings.filterwarnings("ignore")

import numpy as np
import torch
import webdataset as wbs
from torch.multiprocessing import Pool, set_start_method
from tqdm import tqdm

from .data import SpatialTemporalData
from .functional import (
    calc_bbox_center,
    gen_edge_attr_s,
    gen_edge_attr_t,
    gen_edge_index,
)
from .transform import FlowToTensor, FrameToTensor, NormalizeX

set_start_method("spawn", force=True)
sys.path.append("src")
from model import HumanTracking
from utils import json_handler, video


class ShardWritingManager(SyncManager):
    @staticmethod
    def calc_ndarray_size(shape, dtype):
        return np.prod(shape) * np.dtype(dtype).itemsize

    @classmethod
    def create_shared_ndarray(cls, name, shape, dtype):
        size = cls.calc_ndarray_size(shape, dtype)
        shm = shared_memory.SharedMemory(name=name, create=True, size=size)
        return np.ndarray(shape, dtype, shm.buf), shm


def check_full(tail_frame, tail_ind, head, que_len):
    is_frame_que_full = (tail_frame.value + 1) % que_len == head.value
    is_ind_que_full = (tail_ind.value + 1) % que_len == head.value
    is_eq = tail_frame.value == tail_ind.value
    return is_frame_que_full and is_ind_que_full and is_eq


def _optical_flow_async(lock, cap, frame_que, flow_que, tail_frame, head, pbar):
    que_len = frame_que.shape[0]

    with lock:
        prev_frame = cap.read(0)[1]
        cap.set_pos_frame_count(0)  # reset read position
        frame_count = cap.get_frame_count()

    for n_frame in range(frame_count):
        with lock:
            frame = cap.read(n_frame)[1]
        flow = video.optical_flow(prev_frame, frame)
        prev_frame = frame

        with lock:
            frame_que[tail_frame.value] = frame
            flow_que[tail_frame.value] = flow
            pbar.update()

        if n_frame + 1 == frame_count:
            break  # finish

        next_tail = (tail_frame.value + 1) % que_len
        while next_tail == head.value:
            time.sleep(0.01)
        tail_frame.value = next_tail


def _human_tracking_async(
    lock, cap, ind_que, tail_ind, head, pbar, json_path, model=None
):
    que_len = len(ind_que)

    do_human_tracking = not os.path.exists(json_path)
    if not do_human_tracking:
        json_data = json_handler.load(json_path)

    with lock:
        frame_count = cap.get_frame_count()

    for n_frame in range(frame_count):
        if do_human_tracking:
            with lock:
                frame = cap.read(n_frame)[1]
            inds_tmp = model.predict(frame, n_frame)
        else:
            inds_tmp = [ind for ind in json_data if ind["n_frame"] == n_frame]

        with lock:
            ind_que[tail_ind.value] = inds_tmp
            pbar.update()

        if n_frame + 1 == frame_count:
            break  # finish

        next_tail = (tail_ind.value + 1) % que_len
        while next_tail == head.value:
            time.sleep(0.01)
        tail_ind.value = next_tail

    if model is not None:
        del model


def _write_async(
    n_frame, head_val, lock, sink, frame_que, flow_que, ind_que, pbar, video_num
):
    with lock:
        # copy queue
        copy_frame_que = frame_que.copy()
        copy_flow_que = flow_que.copy()
        copy_ind_que = list(ind_que)

    que_len = len(copy_ind_que)
    assert n_frame % que_len == head_val

    sorted_idxs = list(range(head_val, que_len)) + list(range(0, head_val))
    unique_ids = set(
        itertools.chain.from_iterable(
            [[ind["id"] for ind in inds] for inds in copy_ind_que]
        )
    )

    # create individual data
    inds_dict = {_id: {"bbox": [], "keypoints": []} for _id in unique_ids}
    for t, idx in enumerate(sorted_idxs):
        inds_tmp = copy_ind_que[idx]
        ids_tmp = [ind["id"] for ind in inds_tmp]
        for key_id in unique_ids:
            if key_id in ids_tmp:
                ind = [ind for ind in inds_tmp if ind["id"] == key_id][0]
                inds_dict[key_id]["bbox"].append(
                    np.array(ind["bbox"], dtype=np.float16)[:4]
                )
                inds_dict[key_id]["keypoints"].append(
                    np.array(ind["keypoints"], dtype=np.float16)[:, :2]
                )
            else:
                # append dmy
                inds_dict[key_id]["bbox"].append(np.full((4,), -1, dtype=np.float16))
                inds_dict[key_id]["keypoints"].append(
                    np.full((17, 2), -1, dtype=np.float16)
                )

    # create group data
    node_dict = {}
    node_idxs_s = []
    node_idxs_t = {_id: [] for _id in unique_ids}
    node_idx = 0
    for t, idx in enumerate(sorted_idxs):
        inds_tmp = copy_ind_que[idx]
        node_idxs_s.append([])
        for ind in inds_tmp:
            _id = ind["id"]
            node_dict[node_idx] = (
                t,
                _id,
                np.array(ind["bbox"], dtype=np.float16)[:4],
                np.array(ind["keypoints"], dtype=np.float16)[:, :2],
            )
            node_idxs_s[t].append(node_idx)
            node_idxs_t[_id].append(node_idx)
            node_idx += 1

    data = {
        "__key__": f"{video_num}_{n_frame}",
        "npz": {
            "frame": copy_frame_que[sorted_idxs],
            "optical_flow": copy_flow_que[sorted_idxs],
        },
        "individuals.pickle": inds_dict,
        "group.pickle": (node_dict, node_idxs_s, node_idxs_t),
    }
    sink.write(data)

    # pbar.write(f"Complete writing n_frame:{n_frame}")
    pbar.update()
    del data
    gc.collect()


def create_shards(
    video_path: str,
    config: SimpleNamespace,
    config_human_tracking: SimpleNamespace,
    device: str,
    n_processes: int = None,
):
    if n_processes is None:
        n_processes = os.cpu_count()

    data_root = os.path.dirname(video_path)
    video_num = os.path.basename(video_path).split(".")[0]
    dir_path = os.path.join(data_root, video_num)

    json_path = os.path.join(dir_path, "json", "pose.json")
    if not os.path.exists(json_path):
        model_ht = HumanTracking(config_human_tracking, device)
    else:
        model_ht = None

    shard_maxsize = float(config.max_shard_size)
    seq_len = int(config.seq_len)
    stride = int(config.stride)
    shard_pattern = f"dstg-w{seq_len}-s{stride}" + "-%05d.tar"

    shard_pattern = os.path.join(dir_path, "shards", shard_pattern)
    os.makedirs(os.path.dirname(shard_pattern), exist_ok=True)

    ShardWritingManager.register("Tqdm", tqdm)
    ShardWritingManager.register("Capture", video.Capture)
    ShardWritingManager.register("ShardWriter", wbs.ShardWriter)
    with Pool(n_processes) as pool, ShardWritingManager() as swm:
        async_results = []

        lock = swm.Lock()
        cap = swm.Capture(video_path)
        frame_count, img_size = cap.get_frame_count(), cap.get_size()
        head = swm.Value("i", 0)

        # create progress bars
        pbar_of = swm.Tqdm(
            total=frame_count, ncols=100, desc="optical flow", position=1, leave=False
        )
        pbar_ht = swm.Tqdm(
            total=frame_count, ncols=100, desc="human tracking", position=2, leave=False
        )
        total = (frame_count - seq_len) // stride
        pbar_w = swm.Tqdm(
            total=total, ncols=100, desc="writing", position=3, leave=False
        )

        # create shared ndarray and start optical flow
        shape = (seq_len, img_size[1], img_size[0], 3)
        frame_que, frame_shm = swm.create_shared_ndarray("frame", shape, np.uint8)
        shape = (seq_len, img_size[1], img_size[0], 2)
        flow_que, flow_shm = swm.create_shared_ndarray("flow", shape, np.float16)
        tail_frame = swm.Value("i", 0)
        pool.apply_async(
            _optical_flow_async,
            (lock, cap, frame_que, flow_que, tail_frame, head, pbar_of),
        )

        # create shared list of indiciduals and start human tracking
        ind_que = swm.list([[] for _ in range(seq_len)])
        tail_ind = swm.Value("i", 0)
        pool.apply_async(
            _human_tracking_async,
            (lock, cap, ind_que, tail_ind, head, pbar_ht, json_path, model_ht),
        )

        # create shard writer and start writing
        sink = swm.ShardWriter(shard_pattern, shard_maxsize, verbose=0)
        write_async_partial = functools.partial(
            _write_async,
            lock=lock,
            sink=sink,
            frame_que=frame_que,
            flow_que=flow_que,
            ind_que=ind_que,
            pbar=pbar_w,
            video_num=video_num,
        )
        check_full_partial = functools.partial(
            check_full,
            tail_frame=tail_frame,
            tail_ind=tail_ind,
            head=head,
            que_len=seq_len,
        )
        for n_frame in range(seq_len, frame_count, stride):
            while not check_full_partial():
                time.sleep(0.01)

            # start writing
            # pbar_w.write(f"Start writing n_frame:{n_frame}")
            result = pool.apply_async(
                write_async_partial,
                (n_frame, head.value),
            )
            async_results.append(result)
            head.value = (head.value + stride) % seq_len

        while [r.wait() for r in async_results].count(True) > 0:
            time.sleep(0.01)
        pbar_of.close()
        pbar_ht.close()
        pbar_w.close()
        frame_shm.unlink()
        frame_shm.close()
        flow_shm.unlink()
        flow_shm.close()
        sink.close()
        del frame_que, flow_que


def _npz_to_tensor(npz, frame_trans, flow_trans):
    frames, flows = list(npz.values())
    frames = frame_trans(frames)
    flows = flow_trans(flows)

    return torch.cat([frames, flows], dim=1).contiguous()


_partial_npz_to_tensor = functools.partial(
    _npz_to_tensor, frame_trans=FrameToTensor(), flow_trans=FlowToTensor()
)


def _extract_individual_features(pkl, kps_norm_func):
    inds_dict, _, _ = pkl
    bboxs = [ind[2] for ind in inds_dict.values()]  # bbox
    kps = [kps_norm_func(ind[3]) for ind in inds_dict.values()]  # keypoints
    return bboxs, kps


def _create_graph(pkl, kps_norm_func, has_edge_attr):
    inds_dict, node_idxs_s, node_idxs_t = pkl
    x = [kps_norm_func(ind[3]) for ind in inds_dict.values()]  # keypoints
    y = [ind[1] for ind in inds_dict.values()]  # id
    pos = [calc_bbox_center(ind[2]) for ind in inds_dict.values()]
    time = [ind[0] for ind in inds_dict.values()]  # t
    edge_index_s = gen_edge_index(node_idxs_s)
    edge_index_t = gen_edge_index(list(node_idxs_t.values()))
    if has_edge_attr:
        edge_attr_s = gen_edge_attr_s(pos, edge_index_s)
        edge_attr_t = gen_edge_attr_t(pos, time, edge_index_t)
        edge_attr_s = torch.tensor(edge_attr_s, dtype=torch.float32)
        edge_attr_t = torch.tensor(edge_attr_t, dtype=torch.float32)
    else:
        edge_attr_s = None
        edge_attr_t = None

    return SpatialTemporalData(
        torch.tensor(x, dtype=torch.float32),
        torch.tensor(y, dtype=torch.long),
        torch.tensor(pos, dtype=torch.float32),
        torch.tensor(time, dtype=torch.long),
        torch.tensor(edge_index_s, dtype=torch.long),
        edge_attr_s,
        torch.tensor(edge_index_t, dtype=torch.long),
        edge_attr_t,
    )


def load_dataset(
    data_root: str, dataset_type: str, kps_norm_type: str, has_edge_attr: bool = True
):
    shard_paths = []
    data_dirs = sorted(glob(os.path.join(data_root, "*/")))
    for dir_path in data_dirs:
        shard_paths += sorted(glob(os.path.join(dir_path, "shards", "*.tar")))

    dataset = wbs.WebDataset(shard_paths).decode().to_tuple("npz", "pickle")
    if dataset_type == "individual":
        partial_extract_individual_features = functools.partial(
            _extract_individual_features,
            kps_norm_func=NormalizeX(kps_norm_type),
        )
        dataset = dataset.map_tuple(
            _partial_npz_to_tensor, partial_extract_individual_features
        )
    elif dataset_type == "group":
        partial_create_graph = functools.partial(
            _create_graph,
            norm_func=NormalizeX(kps_norm_type),
            has_edge_attr=has_edge_attr,
        )
        dataset = dataset.map_tuple(_partial_npz_to_tensor, partial_create_graph)
    else:
        raise ValueError

    return dataset
