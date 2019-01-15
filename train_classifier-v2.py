#!/usr/bin/env python

"""
    train_classifier.py
"""

import os
import sys
import json
import torch
import argparse
import numpy as np
import pandas as pd
from functools import partial

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SequentialSampler

from basenet.helpers import set_seeds, set_freeze
from basenet.text.data import RaggedDataset, SortishSampler, text_collate_fn

from ulmfit import TextClassifier, basenet_train

bptt, emb_sz, n_hid, n_layers, batch_size = 70, 400, 1150, 3, 48
dps = np.array([0.4, 0.5, 0.05, 0.3, 0.1])
lr  = 3e-3
lrm = 2.6
lrs = np.array([lr / (lrm ** i) for i in range(5)[::-1]])
max_seq = 20 * 70
pad_token = 1

# --
# CLI

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lm-weights-path', type=str, default='results/ag/lm_ft_final-epoch0.h5')
    
    parser.add_argument('--df-path',  type=str, default='data/ag.tsv')
    parser.add_argument('--doc-path', type=str, default='results/ag.id.npy')
    parser.add_argument('--outpath',  type=str, default='results/ag/')
    
    parser.add_argument('--seed', type=int, default=123)
    
    parser.add_argument('--train-size', type=int, default=500)
    parser.add_argument('--valid-size', type=int, default=None)
    
    return parser.parse_args()


# --
# Params

args = parse_args()
set_seeds(args.seed)

os.makedirs(args.outpath, exist_ok=True)

# --
# IO

def load_cl_docs(df_path, doc_path):
    docs = np.load(doc_path)
    return docs[train_sel], docs[~train_sel]

docs = np.load(args.doc_path)
train_sel, label = pd.read_csv(args.df_path, sep='\t', usecols=['cl_train', 'label']).values.T
train_sel = train_sel.astype(bool)

X_train, X_valid = docs[train_sel], docs[~train_sel]
y_train, y_valid = label[train_sel], label[~train_sel]

if args.train_size:
    print('subset training data to %d records' % args.train_size, file=sys.stderr)
    train_sel = np.random.choice(X_train.shape[0], args.train_size, replace=False)
    X_train, y_train = X_train[train_sel], y_train[train_sel]

if args.valid_size:
    print('subset valid data to %d records' % args.valid_size, file=sys.stderr)
    valid_sel = np.random.choice(X_valid.shape[0], args.valid_size, replace=False)
    X_valid, y_valid = X_valid[valid_sel], y_valid[valid_sel]


# Map labels to sequential ints
ulabs   = np.unique(y_train)
n_class = len(ulabs)

lab_lookup = dict(zip(ulabs, range(len(ulabs))))
y_train    = np.array([lab_lookup[l] for l in y_train])
y_valid    = np.array([lab_lookup[l] for l in y_valid])

# Sort validation data by length, longest to shortest, for efficiency
o = np.argsort([len(x) for x in X_valid])[::-1]
X_valid, y_valid = X_valid[o], y_valid[o]

dataloaders = {
    "train" : DataLoader(
        dataset=RaggedDataset(X_train, y_train),
        sampler=SortishSampler(X_train, batch_size=batch_size//2),
        batch_size=batch_size//2,
        collate_fn=text_collate_fn,
        num_workers=1,
        pin_memory=True,
    ),
    "valid" : DataLoader(
        dataset=RaggedDataset(X_valid, y_valid),
        sampler=SequentialSampler(X_valid),
        batch_size=batch_size,
        collate_fn=text_collate_fn,
        num_workers=1,
        pin_memory=True,
    )
}

# --
# Define model

def text_classifier_loss_fn(x, target, alpha=0, beta=0):
    assert isinstance(x, tuple), 'not isinstance(x, tuple)'
    assert len(x) == 3, 'len(x) != 3'
    
    l_x, last_raw_output, last_output = x
    
    # Cross entropy loss
    loss = F.cross_entropy(l_x, target)
    
    # Activation Regularization
    if alpha:
        loss = loss + sum(alpha * last_output.pow(2).mean())
    
    # Temporal Activation Regularization (slowness)
    if beta: 
        if len(last_raw_output) > 1:
            loss = loss + sum(beta * (last_raw_output[1:] - last_raw_output[:-1]).pow(2).mean())
    
    return loss


lm_weights = torch.load(args.lm_weights_path)
n_tok = lm_weights['encoder.encoder.weight'].shape[0]

model = TextClassifier(
    bptt        = bptt,
    max_seq     = max_seq,
    n_class     = n_class,
    n_tok       = n_tok,
    emb_sz      = emb_sz,
    n_hid       = n_hid,
    n_layers    = n_layers,
    pad_token   = pad_token,
    head_layers = [emb_sz * 3, 50, n_class],
    head_drops  = [dps[4], 0.1],
    dropouti    = dps[0],
    wdrop       = dps[1],
    dropoute    = dps[2],
    dropouth    = dps[3],
    loss_fn     = partial(text_classifier_loss_fn, alpha=2, beta=1),
).to('cuda')
model.verbose = True
print(model, file=sys.stderr)

# >>
# !! Should maybe save encoder weights separately in `finetune_lm.py`
weights_to_drop = [k for k in lm_weights.keys() if 'decoder.' in k]
for k in weights_to_drop:
    del lm_weights[k]
# <<

model.load_state_dict(lm_weights, strict=False)
set_freeze(model, False)

set_freeze(model.encoder.encoder, True)
set_freeze(model.encoder.dropouti, True)
set_freeze(model.encoder.rnns, True)
set_freeze(model.encoder.dropouths, True)

# >>

model.reset()
_ = model.eval()
model.use_decoder = False

x = next(iter(dataloaders['train']))[0].cuda()
model.encoder(x)

from basenet.helpers import to_numpy
from tqdm import tqdm

def extract_feats(model, dataloaders, mode):
    all_embs, all_targets = [], []
    for x, y in tqdm(dataloaders[mode], total=len(dataloaders[mode])):
        last_output = model.encoder(x.cuda())[1][-1]
        emb = torch.cat([
            last_output[-1],
            last_output.max(dim=0)[0],
            last_output.mean(dim=0)
        ], 1)
        all_embs.append(to_numpy(emb))
        all_targets.append(to_numpy(y))
        
    return np.vstack(all_embs), np.hstack(all_targets)


train_emb, train_target = extract_feats(model, dataloaders, mode='train')
valid_emb, valid_target = extract_feats(model, dataloaders, mode='valid')

from sklearn.svm import LinearSVC
from sklearn.ensemble import RandomForestClassifier
from sklearn import metrics
from sklearn.feature_extraction.text import TfidfVectorizer

# model = RandomForestClassifier(n_estimators=512, n_jobs=32).fit(train_emb, train_target)
# (valid_target == model.predict(valid_emb)).mean()

# model = LinearSVC(C=0.01).fit(train_emb, train_target)
# (valid_target == model.predict(valid_emb)).mean()

model = LinearSVC(C=0.1).fit(train_emb, train_target)
(valid_target == model.predict(valid_emb)).mean()


df = pd.read_csv(args.df_path, sep='\t')

S_train, S_test = df.text[df.cl_train].values[train_sel], df.text[~df.cl_train].values
z_train, z_test = df.label[df.cl_train].values[train_sel], df.label[~df.cl_train].values

vect = TfidfVectorizer(ngram_range=(1, 2), max_features=30000)
Sv_train = vect.fit_transform(S_train)
Sv_test  = vect.transform(S_test)
(z_test == LinearSVC(C=1000).fit(Sv_train, z_train).predict(Sv_test)).mean()

# # <<

# # --
# # Train

# # Finetune decoder
# set_freeze(model.encoder.encoder, True)
# set_freeze(model.encoder.dropouti, True)
# set_freeze(model.encoder.rnns, True)
# set_freeze(model.encoder.dropouths, True)

# class_ft_dec = basenet_train(
#     classifier,
#     dataloaders,
#     num_epochs=1,
#     lr_breaks=[0, 1/3, 1],
#     lr_vals=[lrs / 8, lrs, lrs / 8],
#     adam_betas=(0.7, 0.99),
#     weight_decay=0,
#     clip_grad_norm=25,
#     save_prefix=os.path.join(args.outpath, 'cl_ft_last1'),
# )

# # Finetune last layer
# set_freeze(classifier.encoder.rnns[-1], False)
# set_freeze(classifier.encoder.dropouths[-1], False)
# class_ft_last = basenet_train(
#     classifier,
#     dataloaders,
#     num_epochs=1,
#     lr_breaks=[0, 1/3, 1],
#     lr_vals=[lrs / 8, lrs, lrs / 8],
#     adam_betas=(0.7, 0.99),
#     weight_decay=0,
#     clip_grad_norm=25,
#     save_prefix=os.path.join(args.outpath, 'cl_ft_last2'),
# )

# # Finetune end-to-end
# set_freeze(classifier, False)
# class_ft_all = basenet_train(
#     classifier,
#     dataloaders,
#     num_epochs=14,
#     lr_breaks=[0, 14/10, 14],
#     lr_vals=[lrs / 32, lrs, lrs / 32],
#     adam_betas=(0.7, 0.99),
#     weight_decay=0,
#     clip_grad_norm=25,
#     save_prefix=os.path.join(args.outpath, 'cl_final'),
# )
