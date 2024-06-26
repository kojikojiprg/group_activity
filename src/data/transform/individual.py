import io

import numpy as np
import torch


def collect_human_tracking(human_tracking_data, unique_ids):
    meta = []
    ids = []
    bboxs = []
    kps = []
    for t, idvs in enumerate(human_tracking_data):
        for idv in idvs:
            i = unique_ids.index(idv["id"])
            meta.append([t, i])
            ids.append(idv["id"])
            bboxs.append(np.array(idv["bbox"], dtype=np.float32)[:4].reshape(2, 2))
            kps.append(np.array(idv["keypoints"], dtype=np.float32)[:, :2])

    return (
        np.array(meta, np.uint16),
        np.array(ids, np.uint16),
        np.array(bboxs, np.float32),
        np.array(kps, np.float32),
    )


def individual_to_npz(meta, unique_ids, frames, flows, bboxs, kps, frame_size):
    h, w = frames.shape[1:3]
    seq_len, n = np.max(meta, axis=0) + 1
    frames_idvs = np.full((n, seq_len, h, w, 3), 0, dtype=np.uint8)
    flows_idvs = np.full((n, seq_len, h, w, 2), -1e10, dtype=np.float32)
    bboxs_idvs = np.full((n, seq_len, 2, 2), -1e10, dtype=np.float32)
    kps_idvs = np.full((n, seq_len, 17, 2), -1e10, dtype=np.float32)

    # collect data
    for (t, i), frames_i, flows_i, bboxs_i, kps_i in zip(
        meta, frames, flows, bboxs, kps
    ):
        frames_idvs[i, t] = frames_i
        flows_idvs[i, t] = flows_i
        bboxs_idvs[i, t] = bboxs_i
        kps_idvs[i, t] = kps_i

    idvs = []
    for i, _id in enumerate(unique_ids):
        data = {
            "id": _id,
            "frame": frames_idvs[i],
            "flow": flows_idvs[i],
            "bbox": bboxs_idvs[i],
            "keypoints": kps_idvs[i],
            "frame_size": frame_size,  # (h, w)
        }
        idvs.append(data)
    return idvs


def individual_npz_to_tensor(
    sample,
    seq_len,
    frame_transform,
    flow_transform,
    bbox_transform,
    kps_transform,
):
    key = sample["__key__"]
    npz = list(np.load(io.BytesIO(sample["npz"])).values())
    _id, frames, flows, bboxs, kps, frame_size = npz

    if len(bboxs) < seq_len:
        # padding
        pad_shape = ((0, seq_len - len(bboxs)), (0, 0), (0, 0), (0, 0))
        # frames = np.pad(frames, pad_shape, constant_values=-1)
        # flows = np.pad(flows, pad_shape, constant_values=-1e10)
        pad_shape = ((0, seq_len - len(bboxs)), (0, 0), (0, 0))
        bboxs = np.pad(bboxs, pad_shape, constant_values=-1e10)
        kps = np.pad(kps, pad_shape, constant_values=-1e10)

    # frames = frame_transform(frames)
    # flows = flow_transform(flows)
    # pixcels = torch.cat([frames, flows], dim=1).to(torch.float32)

    mask = torch.from_numpy(np.any(bboxs < 0, axis=(1, 2))).to(torch.bool)

    kps[~mask] = kps_transform(kps[~mask], bboxs[~mask])
    kps = torch.from_numpy(kps).to(torch.float32)

    bboxs[~mask] = bbox_transform(bboxs[~mask], frame_size[::-1])  # frame_size: (h, w)
    bboxs = torch.from_numpy(bboxs).to(torch.float32)

    del sample, npz, frames, flows  # release memory

    # return key, _id, pixcels, bboxs, mask
    return key, _id, kps, bboxs, mask
