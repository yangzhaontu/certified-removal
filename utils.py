# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import torch
import math
from fast_grad.goodfellow_backprop import goodfellow_backprop

# extracts features into a tensor
def extract_features(extr, device, data_loader):
    extr.eval()
    features = None
    labels = None
    with torch.no_grad():
        for batch_idx, (data, target) in enumerate(data_loader):
            data, target = data.to(device), target.to(device)
            output = extr(data).data.cpu()
            if features is None:
                features = output.squeeze()
                labels = target
            else:
                features = torch.cat([features, output.squeeze()], dim=0)
                labels = torch.cat([labels, target], dim=0)
    return features, labels

# computes per-example gradient of the extractor and classifier models
# clf must be a FastGradMLP
def per_example_gradient(extr, clf, x, y, loss_fn, include_linear=False):
    logits, activations, linearCombs = clf(extr(x))
    loss = loss_fn(logits, y)
    loss.backward(retain_graph=True)
    gradients = []
    for module in list(next(extr.children()).children()):
        grad = module.expand_func.weight.grad * x.size(0)
        gradients.append(grad.view(x.size(0), -1, grad.size(1), grad.size(2), grad.size(3)))
        if module.expand_func.bias is not None:
            gradients.append(module.expand_func.bias.grad.view(x.size(0), -1) * x.size(0))
    if include_linear:
        linearGrads = torch.autograd.grad(loss, linearCombs)
        linearGrads = goodfellow_backprop(activations, linearGrads)
        gradients = gradients + linearGrads
    return loss, gradients

# clips each gradient to norm C and sum
def clip_and_sum_gradients(gradients, C):
    grad_vec = batch_grads_to_vec(gradients)
    grad_norm = grad_vec.norm(2, 1)
    multiplier = grad_norm.new(grad_norm.size()).fill_(1)
    multiplier[grad_norm.gt(C)] = C / grad_norm[grad_norm.gt(C)]
    grad_vec *= multiplier.unsqueeze(1)
    return grad_vec.sum(0)

# adds noise to computed gradients
# grad_vec should be average of gradients
def add_noisy_gradient(extr, clf, device, grad_vec, C, std, include_linear=False):
    noise = torch.randn(grad_vec.size()).to(device) * C * std
    grad_perturbed = grad_vec + noise
    extr.zero_grad()
    for param in extr.parameters():
        size = param.data.view(1, -1).size(1)
        param.grad = grad_perturbed[:size].view_as(param.data).clone()
        grad_perturbed = grad_perturbed[size:]
    if include_linear:
        clf.zero_grad()
        for param in clf.parameters():
            size = param.data.view(1, -1).size(1)
            param.grad = grad_perturbed[:size].view_as(param.data).clone()
            grad_perturbed = grad_perturbed[size:]
    return noise

# computes L2 regularized loss
def loss_with_reg(model, data, target, loss_fn, lam):
    model.zero_grad()
    loss = loss_fn(model(data), target)
    if lam > 0:
        for param in model.parameters():
            loss += lam * param.pow(2).sum() / 2
    loss.backward()
    return loss

# computes average gradient of the full dataset
def compute_full_grad(model, device, data_loader, loss_fn, lam=0):
    full_grad = None
    model.zero_grad()
    for batch_idx, (data, target) in enumerate(data_loader):
        data, target = data.to(device), target.to(device)
        loss_with_reg(model, data, target, loss_fn, lam)
        grad = params_to_vec(model.parameters(), grad=True)
        if full_grad is None:
            full_grad = grad * data.size(0) / len(data_loader.dataset)
        else:
            full_grad += grad * data.size(0) / len(data_loader.dataset)
        model.zero_grad()
    param_vec = params_to_vec(model.parameters())
    return full_grad, param_vec

def params_to_vec(parameters, grad=False):
    vec = []
    for param in parameters:
        if grad:
            vec.append(param.grad.view(1, -1))
        else:
            vec.append(param.data.view(1, -1))
    return torch.cat(vec, dim=1).squeeze()

def vec_to_params(vec, parameters):
    param = []
    for p in parameters:
        size = p.view(1, -1).size(1)
        param.append(vec[:size].view(p.size()))
        vec = vec[size:]
    return param

def batch_grads_to_vec(parameters):
    N = parameters[0].shape[0]
    vec = []
    for param in parameters:
        vec.append(param.view(N,-1))
    return torch.cat(vec, dim=1)

def batch_vec_to_grads(vec, parameters):
    grads = []
    for param in parameters:
        size = param.view(param.size(0), -1).size(1)
        grads.append(vec[:, :size].view_as(param))
        vec = vec[:, size:]
    return grads
