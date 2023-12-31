import random
from typing import Any
import numpy as np
import torch
import torchvision

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

def init_gaussian_dense(dims, stddev, device):
    return torch.empty(dims, requires_grad=False, device=device).normal_(mean=0.0, std=stddev)

class GNCN_PDH:
    def __init__(self, L, dim_top, dim_hid, dim_inp, weight_stddev, beta=0.1, gamma=0.001, alpha_m=0, fn_phi_name='relu', fn_g_hid_name='relu', fn_g_out_name='sigmoid', device=None):
        self.L = L
        self.dim_top = dim_top
        self.dim_hid = dim_hid
        self.dim_inp = dim_inp
        self.beta = beta
        self.gamma = gamma # leak coefficient

        self.device = torch.device('cpu') if device is None else device

        self.W = ([init_gaussian_dense([dim_hid, dim_inp], weight_stddev, self.device)]
            + [init_gaussian_dense([dim_hid, dim_hid], weight_stddev, self.device) for _ in range(L-2)]
            + [init_gaussian_dense([dim_top, dim_hid], weight_stddev, self.device)])

        self.E = ([init_gaussian_dense([dim_inp, dim_hid], weight_stddev, self.device)]
            + [init_gaussian_dense([dim_hid, dim_hid], weight_stddev, self.device) for _ in range(L-1)])

        if alpha_m != 0:
            raise NotImplementedError("Only alpha_m = 0 is supported.")

        if fn_phi_name == 'relu':
            self.fn_phi = torch.relu
        else:
            raise NotImplementedError("Only relu is supported for phi.")

        if fn_g_hid_name == 'relu':
            self.fn_g_hid = torch.relu
        else:
            raise NotImplementedError("Only relu is supported for g_hid.")

        if fn_g_out_name == 'sigmoid':
            self.fn_g_out = torch.sigmoid
        else:
            raise NotImplementedError("Only sigmoid is supported for g_out.")

        self.clip_weights()


    def parameters(self):
        return self.W + self.E


    def infer(self, x, K=50):
        batch_size = x.shape[0]
        z = [x]
        e = [torch.zeros([batch_size, self.dim_inp], device=self.device)]
        for l in range(self.L - 1):
            z.append(torch.zeros([batch_size, self.dim_hid], device=self.device))
            e.append(torch.zeros([batch_size, self.dim_hid], device=self.device))
        z.append(torch.zeros([batch_size, self.dim_top], device=self.device))
        e.append(torch.zeros([batch_size, self.dim_top], device=self.device))

        mu = [None for _ in range(self.L)]

        for _ in range(K):
            for i in range(1, self.L + 1):
                di = e[i-1] @ self.E[i-1] - e[i]
                z[i] += self.beta * (-self.gamma * z[i] + di)

            mu[0] = self.fn_g_out(self.fn_phi(z[1]) @ self.W[0])
            e[0] = z[0] - mu[0]
            for i in range(1, self.L):
                mu[i] = self.fn_g_hid(self.fn_phi(z[i+1]) @ self.W[i])
                e[i] = self.fn_phi(z[i]) - mu[i]

        self.z = z
        self.e = e

    def calc_updates(self):
        batch_size = self.z[0].shape[0]
        avg_factor = -1.0 / (batch_size)

        for l in range(0, self.L):
            dWl = self.fn_phi(self.z[l+1]).T @ self.e[l]

            dWl = avg_factor * dWl
            dEl = dWl.T

            self.W[l].grad = dWl
            self.E[l].grad = dEl

    def clip_weights(self):
        # clip column norms to 1
        for l in range(self.L):
            Wl_col_norms = self.W[l].norm(dim=0, keepdim=True)
            self.W[l].copy_(self.W[l] / torch.maximum(Wl_col_norms, torch.tensor(1.0)))
            El_col_norms = self.E[l].norm(dim=0, keepdim=True)
            self.E[l].copy_(self.E[l] / torch.maximum(El_col_norms, torch.tensor(1.0)))


    def calc_total_discrepancy(self):
        return sum([torch.sum(e**2) for e in self.e[:3]])



class Binarize(object):
    def __init__(self, threshold=0.5):
        self.threshold = threshold

    def __call__(self, data):
        return (data >= self.threshold).float()


def make_moving_collate_fn(device):
    def moving_collate(batch):
        inputs, targets = zip(*batch)
        inputs = torch.stack(inputs).to(device)
        targets = torch.tensor(targets).to(device)
        return inputs, targets

    return moving_collate


def preprocess_binary_mnist(batch_size, device):
    transforms = torchvision.transforms.Compose([torchvision.transforms.ToTensor(), Binarize()])
    data_train = torchvision.datasets.MNIST(root='./data', train=True, download=True, transform=transforms)

    moving_collate = make_moving_collate_fn(device)

    loader_train = torch.utils.data.DataLoader(data_train, batch_size=batch_size, shuffle=True, collate_fn=moving_collate)
    return loader_train


def run_ngc(seed):
    set_seed(seed)

    num_epochs = 50
    batch_size = 512
    lr = 0.001
    dim_inp = 784
    dim_hid = 360
    weight_stddev = 0.05
    L = 3
    K = 50
    beta = 0.1
    gamma = 0.001


    device_name = 'cuda' if torch.cuda.is_available() else 'cpu'
    device = torch.device(device_name)

    loader_train = preprocess_binary_mnist(batch_size, device)

    model = GNCN_PDH(L=L, dim_top=dim_hid, dim_hid=dim_hid, dim_inp=dim_inp, weight_stddev=weight_stddev, beta=beta, gamma=gamma, device=device)

    optimizer = torch.optim.Adam(model.parameters(), lr=lr, maximize=False)

    for epoch in range(num_epochs):
        print(f"--- Epoch {epoch}")
        totd = 0.
        num_samples = 0
        for i, (inputs, targets) in enumerate(loader_train):
            inputs = inputs.view([-1, dim_inp])
            model.infer(inputs, K=K)

            optimizer.zero_grad()

            model.calc_updates()
            totd += model.calc_total_discrepancy()
            num_samples += inputs.shape[0]

            optimizer.step()

            model.clip_weights()
        print(f"Average Total discrepancy: {totd / (1.0 * num_samples)}")



if __name__ == '__main__':
    run_ngc(314159)