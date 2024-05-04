import os
import sys
from glob import glob
from types import SimpleNamespace

import numpy as np
import torch
from scipy.stats import norm
from torch_geometric.data import Data, InMemoryDataset
from tqdm import tqdm

sys.path.append("src")
from utils import json_handler


class GroupActivityDataset(InMemoryDataset):
    def __init__(
        self,
        data_root,
        feature_type,
        config: SimpleNamespace,
        transform=None,
        pre_transform=None,
        pre_filter=None,
    ):
        self._feature_type = feature_type
        self._config = config
        super().__init__(data_root, transform, pre_transform, pre_filter)
        self.load(self.processed_paths[0])

    @property
    def processed_file_names(self):
        return [f"dataset_{self._feature_type}.pt"]

    def _get_feature_shape(self):
        if self._feature_type == "keypoints":
            return (17, 2)
        elif self._feature_type == "bbox":
            return (2, 2)
        else:
            raise ValueError

    @staticmethod
    def _calc_bbox_center(bbox):
        bbox = np.array(bbox).reshape(2, 2)
        return (bbox[1] - bbox[0]) / 2 + bbox[0]

    def process(self):
        # get config
        seq_len = self._config.seq_len
        feature_type = self._feature_type

        dirs = glob(os.path.join(self.root, "*/"))
        dirs = dirs[:1]

        data_list = []
        for dir_path in tqdm(dirs, ncols=100):
            json_path = os.path.join(dir_path, "json", "pose.json")
            pose_data_lst = json_handler.load(json_path)

            # sort data by frame and id
            pose_data_lst = sorted(pose_data_lst, key=lambda x: x["n_frame"])
            max_frame = pose_data_lst[-1]["n_frame"]
            pose_data_lst = sorted(pose_data_lst, key=lambda x: x["id"])

            seq_features = []
            seq_bbox_centers = []
            seq_ids = []
            seq_n_frames = []
            edge_offsets = []
            edge_index = []
            edge_attr = []
            for n_frame in tqdm(range(1, max_frame + 1), leave=False, ncols=100):
                pose_data_frame = [
                    data for data in pose_data_lst if data["n_frame"] == n_frame
                ]
                if len(pose_data_frame) == 0:
                    seq_n_frames.append(n_frame)
                    if len(edge_offsets) > 0:
                        edge_offsets.append(edge_offsets[-1])
                    else:
                        edge_offsets.append(0)
                    continue

                # append data
                if feature_type == "keypoints":
                    features = [
                        np.array(data[feature_type])[:, :2].tolist()
                        for data in pose_data_frame
                    ]
                elif feature_type == "bbox":
                    features = [data[feature_type] for data in pose_data_frame]
                seq_features += features

                bbox_centers = [
                    self._calc_bbox_center(data["bbox"]).tolist()
                    for data in pose_data_frame
                ]
                seq_bbox_centers += bbox_centers

                ids = [data["id"] for data in pose_data_frame]
                edge_offset = len(seq_ids)
                edge_offsets.append(edge_offset)
                seq_ids += ids
                seq_n_frames.append(n_frame)

                # add spatial edge
                adj = np.full((len(ids), len(ids)), 1) - np.eye(len(ids))
                cur_edge_index = (np.array(np.where(adj == 1)).T + edge_offset).tolist()
                edge_index += cur_edge_index
                bc_arr = np.array(seq_bbox_centers)
                for idx in cur_edge_index:
                    diff = np.abs(bc_arr[idx[0]] - bc_arr[idx[1]]) + 1e-10
                    edge_attr.append(np.clip(1 / diff, 0, 1).tolist())

                # add temporal edge
                if seq_len > 0:
                    for j, pre_n_frame in enumerate(seq_n_frames[:-1]):
                        for cur_id_idx, _id in enumerate(ids):
                            pre_eo = edge_offsets[j]
                            nxt_eo = edge_offsets[j + 1]
                            pre_ids = seq_ids[pre_eo : pre_eo + nxt_eo]
                            if _id in pre_ids:
                                # temporal edge_index
                                pre_id_idx = pre_ids.index(_id)
                                pre_idx = pre_eo + pre_id_idx
                                cur_idx = edge_offset + cur_id_idx
                                edge_index += [[pre_idx, cur_idx], [cur_idx, pre_idx]]
                                # temporal edge_attr
                                pre_bc_arr = np.array(seq_bbox_centers)[pre_idx]
                                cur_bc_arr = np.array(seq_bbox_centers)[cur_idx]
                                ws = norm.pdf(pre_bc_arr - cur_bc_arr, 0, 1)
                                wt = norm.pdf(
                                    abs(n_frame - pre_n_frame), 0, seq_len / 2
                                )
                                attr = (ws * wt).tolist()
                                edge_attr += [attr, attr]

                if n_frame < seq_len:
                    continue  # wait for saving seauential data

                # create graph
                fs = self._get_feature_shape()
                graph = Data(
                    torch.tensor(seq_features, dtype=torch.float).view(
                        -1, fs[0], fs[1]
                    ),
                    torch.tensor(edge_index, dtype=torch.float).t().contiguous(),
                    torch.tensor(edge_attr, dtype=torch.float),
                    torch.tensor(seq_ids, dtype=torch.int).view(-1),
                    torch.tensor(seq_bbox_centers, dtype=torch.float).view(-1, 2),
                )
                data_list.append(graph)

                # update edge
                edge_offsets = edge_offsets[1:]
                first_edge_offset = edge_offsets[0]
                edge_offsets = list(map(lambda x: x - first_edge_offset, edge_offsets))
                for ei in range(first_edge_offset):
                    rm_idxs = np.any(np.array(edge_index) == ei, axis=1)
                    edge_index = np.array(edge_index)[~rm_idxs]
                    edge_attr = np.array(edge_attr)[~rm_idxs].tolist()
                edge_index = (edge_index - first_edge_offset).tolist()
                # update data
                seq_features = seq_features[first_edge_offset:]
                seq_bbox_centers = seq_bbox_centers[first_edge_offset:]
                seq_ids = seq_ids[first_edge_offset:]
                seq_n_frames = seq_n_frames[1:]

            del pose_data_lst
            del seq_features, seq_bbox_centers, seq_ids, seq_n_frames
            del edge_offsets, edge_index, edge_attr

        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]

        self.save(data_list, self.processed_paths[0])