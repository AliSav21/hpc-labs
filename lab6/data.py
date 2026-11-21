"""Датасети MedMNIST: таблиця позначок умови і завантаження розбиття з файлу .npz."""

import numpy as np
import torch
from torch.utils.data import TensorDataset

# позначка: (файл, класів K, каналів C)
DATASETS = {
    "Д1": ("dermamnist", 7, 3),
    "Д2": ("bloodmnist", 8, 3),
    "Д3": ("organamnist", 11, 1),
    "Д4": ("organcmnist", 11, 1),
    "Д5": ("pathmnist", 9, 3),
    "Д6": ("octmnist", 4, 1),
}

WIDTHS = {"Ш1": (16, 32, 128), "Ш2": (32, 64, 256), "Ш3": (64, 128, 512)}

ZENODO = "https://zenodo.org/records/10519652/files"


def load_split(path, split, n=None):
    """Розбиття split ("train" або "test") файлу .npz як TensorDataset."""
    d = np.load(path)
    x = d[f"{split}_images"][:n]              # (N, 28, 28) або (N, 28, 28, 3), uint8
    y = d[f"{split}_labels"][:n].reshape(-1)  # (N, 1) -> (N,)
    if x.ndim == 3:
        x = x[:, None, :, :]                  # (N, 1, 28, 28)
    else:
        x = x.transpose(0, 3, 1, 2)           # (N, 3, 28, 28)
    x = torch.from_numpy(np.ascontiguousarray(x)).float()
    x = (x / 255 - 0.5) / 0.5
    return TensorDataset(x, torch.from_numpy(y.astype(np.int64)))
