import torch.nn as nn


class FusionANN(nn.Module):
    """
    Architecture recovered from fusion_ann_v3's state_dict:
    Linear(33->64) -> BN -> ReLU -> Dropout
    Linear(64->32) -> BN -> ReLU -> Dropout
    Linear(32->16) -> BN -> ReLU -> Dropout
    Linear(16->1)
    """

    def __init__(self, input_dim: int = 33, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 64), nn.BatchNorm1d(64), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(64, 32), nn.BatchNorm1d(32), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(32, 16), nn.BatchNorm1d(16), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(16, 1),
        )

    def forward(self, x):
        return self.net(x)
