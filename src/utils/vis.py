import cv2
import matplotlib.pyplot as plt
import numpy as np
from sklearn.manifold import TSNE

EDGE_INDEX = [
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),  # Head
    (5, 6),
    (5, 7),
    (7, 9),
    (6, 8),
    (8, 10),
    (5, 11),
    (6, 12),  # Body
    (11, 13),
    (12, 14),
    (13, 15),
    (14, 16),
]
KP_COLOR = [
    # Nose, LEye, REye, LEar, REar
    (0, 255, 255),
    (0, 191, 255),
    (0, 255, 102),
    (0, 77, 255),
    (0, 255, 0),
    # LShoulder, RShoulder, LElbow, RElbow, LWrist, RWrist
    (77, 255, 255),
    (77, 255, 204),
    (77, 204, 255),
    (191, 255, 77),
    (77, 191, 255),
    (191, 255, 77),
    # LHip, RHip, LKnee, Rknee, LAnkle, RAnkle, Neck
    (204, 77, 255),
    (77, 255, 204),
    (191, 77, 255),
    (77, 255, 191),
    (127, 77, 255),
    (77, 255, 127),
    (0, 255, 255),
]
LIMB_COLOR = [
    (0, 215, 255),
    (0, 255, 204),
    (0, 134, 255),
    (0, 255, 50),
    (77, 255, 222),
    (77, 196, 255),
    (77, 135, 255),
    (191, 255, 77),
    (77, 255, 77),
    (77, 222, 255),
    (255, 156, 127),
    (0, 127, 255),
    (255, 127, 77),
    (0, 77, 255),
    (255, 77, 36),
]


def draw_skeleton(frame: np.array, kps: np.array, color: tuple = None):
    part_line = {}

    # draw keypoints
    for n in range(len(kps)):
        cor_x, cor_y = int(kps[n, 0]), int(kps[n, 1])
        part_line[n] = (cor_x, cor_y)
        c = KP_COLOR[n] if color is None else color
        cv2.circle(frame, (cor_x, cor_y), 3, c, 1)

    # draw limbs
    for start_p, end_p in EDGE_INDEX:
        if start_p in part_line and end_p in part_line:
            start_xy = part_line[start_p]
            end_xy = part_line[end_p]
            c = LIMB_COLOR[n] if color is None else color
            cv2.line(frame, start_xy, end_xy, c, 2, 3)

    return frame


def draw_bbox(frame: np.array, bbox: np.array, color: tuple):
    pt1, pt2 = bbox.astype(int).reshape(2, 2)
    frame = cv2.rectangle(frame, pt1, pt2, color, 2)
    return frame


def plot_on_frame(frame, results, idx_data, frame_size, content):
    cm = plt.get_cmap("tab10")
    for data in results:
        _id = data["id"]
        bbox = data["x_spc"][idx_data]
        mask = data["mask"][idx_data]

        if mask:
            continue

        bbox = (bbox.copy() + 1) / 2 * frame_size

        # id
        pt = tuple(np.mean(bbox, axis=0).astype(int))
        frame = cv2.putText(
            frame, str(_id), pt, cv2.FONT_HERSHEY_COMPLEX, 1, (255, 255, 255), 2
        )

        # bbox = bbox.copy() * frame_size
        if content == "x_vis":
            # fake_img = data["fake_x_vis"][idx_data]
            kps = data["x_vis"][idx_data]
            fake_kps = data["fake_x_vis"][idx_data]
            mse_x_vis = data["mse_x_vis"]

            # resize imgs and write
            # x1, y1, x2, y2 = bbox.reshape(4).astype(int)
            # fake_img = cv2.resize(fake_img, (x2 - x1, y2 - y1))
            # fake_img = fake_img[:, :, :3]
            # mean, std = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
            # fake_img = ((fake_img * std) + mean) * 255
            # frame[y1:y2, x1:x2] = (fake_img).astype(np.uint8)

            kps = (kps.copy() + 1) / 2 * (bbox[1] - bbox[0]) + bbox[0]
            fake_kps = (fake_kps.copy() + 1) / 2 * (bbox[1] - bbox[0]) + bbox[0]
            # kps = kps.copy() * (bbox[1] - bbox[0]) + bbox[0]
            # fake_kps = fake_kps.copy() * (bbox[1] - bbox[0]) + bbox[0]
            frame = draw_skeleton(frame, kps, color=(0, 255, 0))
            frame = draw_skeleton(frame, fake_kps, color=(0, 0, 255))

            # mse
            pt = tuple(np.min(bbox, axis=0).astype(int))  # top-left
            frame = cv2.putText(
                frame,
                f"{mse_x_vis:.3f}",
                pt,
                cv2.FONT_HERSHEY_COMPLEX,
                0.8,
                (255, 255, 255),
                2,
            )
        elif content == "x_spc":
            mse_x_spc = data["mse_x_spc"]
            fake_bbox = data["fake_x_spc"][idx_data]

            # bbox and fake_bbox
            fake_bbox = (fake_bbox.copy() + 1) / 2 * frame_size
            # fake_bbox = fake_bbox.copy() * frame_size
            frame = draw_bbox(frame, bbox, color=(0, 255, 0))
            frame = draw_bbox(frame, fake_bbox, color=(0, 0, 255))

            # mse
            pt = tuple(np.min(bbox, axis=0).astype(int))  # top-left
            frame = cv2.putText(
                frame,
                f"{mse_x_spc:.3f}",
                pt,
                cv2.FONT_HERSHEY_COMPLEX,
                0.8,
                (255, 255, 255),
                2,
            )
        elif content == "cluster":
            label = str(data["label"])

            color = (np.array(cm(int(label))[:3]) * 255).astype(int).tolist()
            color = tuple(color[::-1])  # RGB -> BGR
            frame = draw_bbox(frame, bbox, color)

            # clustering label
            pt = tuple(np.min(bbox, axis=0).astype(int))  # top-left
            frame = cv2.putText(
                frame, label, pt, cv2.FONT_HERSHEY_COMPLEX, 0.8, color, 2
            )
        else:
            raise ValueError

    return frame


def plot_mse(mse_x_dict, frame_count, stride, th, ylabel, figpath=None, is_show=False):
    vals_dict = {}
    for _id, mse_dict in mse_x_dict.items():
        if len(mse_dict) < 2:
            continue
        n_frames = np.array(list(mse_dict.keys()))
        mses = np.array(list(mse_dict.values()))

        idxs = n_frames // stride
        n_samples = frame_count // stride + 1
        vals = np.full((n_samples,), np.nan, np.float32)
        vals[idxs] = mses

        vals_dict[_id] = vals.astype(np.float32)

    ids = list(vals_dict.keys())
    vals = np.array(list(vals_dict.values())).T
    vals = vals.astype(np.float32)
    mse_ratio = np.count_nonzero(vals > th, axis=1) / np.count_nonzero(
        np.nan_to_num(vals), axis=1
    )

    num = 5
    b = np.ones(num) / num
    mse_ratio = np.convolve(mse_ratio, b, mode="same")

    fig = plt.figure(figsize=(12, 4))
    ax1 = fig.add_subplot(1, 1, 1)
    ax1.plot(vals, color="black", linewidth=1, alpha=0.3, label=ids)
    ax1.set_xlim(0, n_samples)
    ax1.set_xlabel("sec")
    ax1.set_ylim(0, 1.0)
    ax1.set_ylabel(ylabel)

    ax2 = ax1.twinx()
    ax2.plot(mse_ratio, color="red", linewidth=1, label="mean")
    ax2.set_ylim(0, 10)
    ax2.set_ylabel(f"ratio (mse>{th})")

    # plt.legend()
    if figpath is not None:
        plt.savefig(figpath, bbox_inches="tight")
    if is_show:
        plt.show()
    plt.close()


def plot_label_count(labels_dict, frame_count, stride, figpath=None, is_show=False):
    vals_dict = {}
    for label, count_dict in labels_dict.items():
        if len(count_dict) < 2:
            continue
        n_frames = np.array(list(count_dict.keys()))
        counts = np.array(list(count_dict.values()))

        idxs = n_frames // stride
        n_samples = frame_count // stride + 1
        vals = np.full((n_samples,), np.nan, np.float32)
        vals[idxs] = counts

        vals_dict[label] = vals

    ids = list(vals_dict.keys())
    vals = np.array(list(vals_dict.values())).T

    fig = plt.figure(figsize=(12, 4))
    ax1 = fig.add_subplot(1, 1, 1)
    ax1.plot(vals, linewidth=1, label=ids)
    ax1.set_xlim(0, n_samples)
    ax1.set_xlabel("sec")
    ax1.set_ylabel("count")
    plt.legend()

    # plt.legend()
    if figpath is not None:
        plt.savefig(figpath, bbox_inches="tight")
    if is_show:
        plt.show()
    plt.close()


def plot_tsne(X, labels, perplexity=10, figpath=None, is_show=False, cmap="tab10", lut=None, legend=True):
    tsne = TSNE(n_components=2, random_state=42, perplexity=perplexity, n_iter=1000)
    embedded = tsne.fit_transform(X)
    unique_labels = np.unique(labels)
    cm = plt.get_cmap(cmap, lut)
    for label in unique_labels:
        x = embedded[labels == label]
        if lut is not None:
            ci = np.where(unique_labels == label)[0].item()
        else:
            ci = int(label)
        c = cm(ci)
        plt.scatter(x.T[0], x.T[1], s=3, c=c, label=label)
    plt.xticks([])
    plt.yticks([])
    if legend:
        plt.legend(bbox_to_anchor=(1.01, 1))
    if figpath is not None:
        plt.savefig(figpath, bbox_inches="tight")
    if is_show:
        plt.show()
    plt.close()
