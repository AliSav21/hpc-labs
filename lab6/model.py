"""Мережа умови: два згорткові шари з ReLU і підвибіркою, прихований шар, вихід на K класів."""

from torch import nn


class Net(nn.Module):
    def __init__(self, C, c1, c2, h, K):
        super().__init__()
        self.features = nn.Sequential(
            # 28 x 28 -> 14 x 14
            nn.Conv2d(C, c1, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            # 14 x 14 -> 7 x 7
            nn.Conv2d(c1, c2, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        )
        self.head = nn.Sequential(
            nn.Flatten(), nn.Linear(7 * 7 * c2, h), nn.ReLU(),
            nn.Linear(h, K),
        )

    def forward(self, x):
        return self.head(self.features(x))


def n_params_formula(C, c1, c2, h, K):
    return 9 * C * c1 + c1 + 9 * c1 * c2 + c2 + 49 * c2 * h + h + h * K + K
