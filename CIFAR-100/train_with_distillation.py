from __future__ import print_function

import os
import time
import numpy as np
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import torch.nn.functional as F
import argparse
import wandb

from datetime import datetime
import distiller
import load_settings

parser = argparse.ArgumentParser(description='CIFAR-100 training')
parser.add_argument('--data_path', type=str, default='../data')
parser.add_argument('--name', type=str)
parser.add_argument('--paper_setting', default='a', type=str)
parser.add_argument('--epochs', default=200, type=int, help='number of total epochs to run')
parser.add_argument('--batch_size', default=128, type=int, help='mini-batch size (default: 256)')
parser.add_argument('--lr', default=0.1, type=float, help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
parser.add_argument('--weight_decay', default=5e-4, type=float, help='weight decay')
parser.add_argument('--alpha', default=0.001, type=float)

parser.add_argument('--connector_depth', default=1, type=int)
parser.add_argument('--connector_bn', action="store_false", default=True)
parser.add_argument('--connector_bias', action="store_true", default=False)
parser.add_argument('--connector_kernel_size', default=1, type=int)

args = parser.parse_args()

gpu_num = 0
use_cuda = torch.cuda.is_available()
transform_train = transforms.Compose([
    transforms.Pad(4, padding_mode='reflect'),
    transforms.RandomHorizontalFlip(),
    transforms.RandomCrop(32),
    transforms.ToTensor(),
    transforms.Normalize(np.array([125.3, 123.0, 113.9]) / 255.0,
                         np.array([63.0, 62.1, 66.7]) / 255.0)
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(np.array([125.3, 123.0, 113.9]) / 255.0,
                         np.array([63.0, 62.1, 66.7]) / 255.0),
])

trainset = torchvision.datasets.CIFAR100(root=args.data_path, train=True, download=True, transform=transform_train)
trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=4)
testset = torchvision.datasets.CIFAR100(root=args.data_path, train=False, download=True, transform=transform_test)
testloader = torch.utils.data.DataLoader(testset, batch_size=args.batch_size, shuffle=False, num_workers=4)

# Model
t_net, s_net, args = load_settings.load_paper_settings(args)

# Module for distillation
d_net = distiller.Distiller(
    t_net,
    s_net,
    connector_depth=args.connector_depth,
    connector_bn=args.connector_bn,
    connector_bias=args.connector_bias,
    connector_kernel_size=args.connector_kernel_size
)

print('the number of teacher model parameters: {}'.format(sum([p.data.nelement() for p in t_net.parameters()])))
print('the number of student model parameters: {}'.format(sum([p.data.nelement() for p in s_net.parameters()])))

if use_cuda:
    torch.cuda.set_device(0)
    d_net.cuda()
    s_net.cuda()
    t_net.cuda()
    cudnn.benchmark = True

criterion_CE = nn.CrossEntropyLoss()

print("wandb init")
def get_timestamp():
    return datetime.now().strftime("%b%d_%H-%M-%S")
wandb.init(
    # Set the project where this run will be logged
    project="knowledge-distillation-overhaul", 
    # We pass a run name (otherwise it’ll be randomly assigned, like sunshine-lollypop-10)
    name=f"experiment_{args.name}-{get_timestamp()}", 
    # Track hyperparameters and run metadata
)
# Training
def train_with_distill(d_net, epoch, alpha):
    epoch_start_time = time.time()
    print('\nDistillation epoch: %d' % epoch)

    d_net.train()
    d_net.s_net.train()
    d_net.t_net.train()

    total_loss = 0
    KD_loss = 0
    CE_loss = 0
    correct = 0
    total = 0

    global optimizer
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        if use_cuda:
            inputs, targets = inputs.cuda(), targets.cuda()
        optimizer.zero_grad()

        batch_size = inputs.shape[0]
        outputs, loss_distill = d_net(inputs)
        loss_CE = criterion_CE(outputs, targets)
        loss_distill = loss_distill.sum() / batch_size

        loss = loss_CE + alpha * loss_distill

        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        CE_loss += loss_CE.item()
        KD_loss += loss_distill.item()

        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().sum().float().item()

        b_idx = batch_idx

    print('Train \t Time Taken: %.2f sec' % (time.time() - epoch_start_time))
    print('total_loss: %.3f | KD Loss: %.3f | CE_Loss: %.3f | Acc: %.3f%% (%d/%d)' % (total_loss / (b_idx + 1), KD_loss / (b_idx + 1), CE_loss / (b_idx + 1), 100. * correct / total, correct, total))


    return total_loss / (b_idx + 1), CE_loss / (b_idx + 1), KD_loss / (b_idx + 1)

def test(net):
    epoch_start_time = time.time()
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(testloader):
        if use_cuda:
            inputs, targets = inputs.cuda(), targets.cuda()
        outputs = net(inputs)
        loss = criterion_CE(outputs, targets)

        test_loss += loss.item()
        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().sum().float().item()
        b_idx = batch_idx

    print('Test \t Time Taken: %.2f sec' % (time.time() - epoch_start_time))
    print('Loss: %.3f | Acc: %.3f%% (%d/%d)' % (test_loss / (b_idx + 1), 100. * correct / total, correct, total))
    return test_loss / (b_idx + 1), correct / total


print('Performance of teacher network')
avg_loss, teacher_acc = test(t_net)

wandb.log({"teacher_acc": teacher_acc, "teacher_avg_loss": avg_loss})

for epoch in range(args.epochs):
    if epoch is 0:
        optimizer = optim.SGD([{'params': s_net.parameters()}, {'params': d_net.Connectors.parameters()}],
                              lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay, nesterov=True)
    elif epoch is (args.epochs // 2):
        optimizer = optim.SGD([{'params': s_net.parameters()}, {'params': d_net.Connectors.parameters()}],
                              lr=args.lr / 10, momentum=args.momentum, weight_decay=args.weight_decay, nesterov=True)
    elif epoch is (args.epochs * 3 // 4):
        optimizer = optim.SGD([{'params': s_net.parameters()}, {'params': d_net.Connectors.parameters()}],
                              lr=args.lr / 100, momentum=args.momentum, weight_decay=args.weight_decay, nesterov=True)

    total_loss, CE_loss, KD_loss = train_with_distill(d_net, epoch, args.alpha)
    test_loss, accuracy = test(s_net)
    wandb.log({"epoch/val_acc": accuracy, "epoch/val_loss": test_loss, "epoch/total_loss": total_loss, "epoch/CE_loss": CE_loss, "epoch/KD_loss": KD_loss, "epoch": epoch})

wandb.finish()
