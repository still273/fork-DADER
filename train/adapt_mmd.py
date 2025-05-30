"""Adversarial adaptation to train target encoder."""
import sys
sys.path.append('../')
import torch
from utils import make_cuda
import torch.nn.functional as F
import torch.nn as nn
import param
import torch.optim as optim
from utils import save_model, init_model
import csv
import os
from metrics import mmd
import numpy as np
import itertools
import time

def train(args, encoder, classifier, src_data_loader,src_valid_loader, tgt_data_train_loader, tgt_data_valid_loader):
    """Train encoder for target domain."""

    # set train state for Dropout and BN layers
    encoder.train()
    classifier.train()
    # setup criterion and optimizer
    BCELoss = nn.BCELoss()
    CELoss = nn.CrossEntropyLoss()
    KLDivLoss = nn.KLDivLoss(reduction='batchmean')
    optimizer = optim.Adam(list(encoder.parameters())+list(classifier.parameters()), lr=param.d_learning_rate)
    len_data_loader = min(len(src_data_loader), len(tgt_data_train_loader))
    bestf1 = 0.0
    besttrainf1 = 0.0
    results = []
    best_epoch = []
    for epoch in range(args.num_epochs):
        t_epoch = time.perf_counter()
        if len(src_data_loader)>len(tgt_data_train_loader):
            data_zip = enumerate(zip(src_data_loader, tgt_data_train_loader))
        else:
            data_zip = enumerate(zip(src_data_loader, tgt_data_train_loader))
        mmd_sum = 0
        jssum = 0
        for step, (src, tgt) in data_zip:
            if tgt:
                reviews_src, src_mask,src_segment, labels,_,_,_ = src
                reviews_tgt, tgt_mask,tgt_segment, _,_,_,_ = tgt
                reviews_src = make_cuda(reviews_src)
                src_mask = make_cuda(src_mask)
                src_segment = make_cuda(src_segment)
                labels = make_cuda(labels)
                reviews_tgt = make_cuda(reviews_tgt)
                tgt_mask = make_cuda(tgt_mask)
                tgt_segment = make_cuda(tgt_segment)
    
                # zero gradients for optimizer
                optimizer.zero_grad()
    
                # extract and concat features
                feat_src = encoder(reviews_src, src_mask, src_segment)
                feat_tgt = encoder(reviews_tgt, tgt_mask, tgt_segment)
                preds = classifier(feat_src)
                cls_loss = CELoss(preds, labels)
                loss_mmd = mmd.mmd_rbf_noaccelerate(feat_src, feat_tgt)
                p = float(step + epoch * len_data_loader) / args.num_epochs / len_data_loader
                lamda = 2. / (1. + np.exp(-10 * p)) - 1
                if args.source_only:
                    loss = cls_loss
                else:
                    loss = cls_loss + args.beta * loss_mmd
            else:
                reviews_src, src_mask,src_segment, labels = src
                reviews_src = make_cuda(reviews_src)
                src_mask = make_cuda(src_mask)
                src_segment = make_cuda(src_segment)
                labels = make_cuda(labels)
                optimizer.zero_grad()
    
                # extract and concat features
                feat_src = encoder(reviews_src, src_mask, src_segment)
                preds = classifier(feat_src)
                cls_loss = CELoss(preds, labels)
                p = float(step + epoch * len_data_loader) / args.num_epochs / len_data_loader
                lamda = 2. / (1. + np.exp(-10 * p)) - 1
                loss = cls_loss
            loss.backward()
            optimizer.step()
            mmd_sum += loss_mmd.item()

            if (step + 1) % args.log_step == 0:
                print("Epoch [%.2d/%.2d] Step [%.3d/%.3d]: "
                      "mmd_loss=%.4f cls_loss=%.4f"
                      % (epoch + 1,
                         args.num_epochs,
                         step + 1,
                         len_data_loader,
                         loss_mmd.item(),
                         cls_loss.item()))
        t_train = time.perf_counter()
        f1_tgt,eloss_train,_,_,_,_ = evaluate(args, encoder, classifier, tgt_data_valid_loader, src_data_loader,epoch=epoch, pattern=1000)
        f1_src, eloss_train, _, _, _, _ = evaluate(args, encoder, classifier, src_valid_loader, src_data_loader,
                                                     epoch=epoch, pattern=1000)
        t_valid = time.perf_counter()
        curr_result = [epoch, f1_src, f1_tgt, t_train-t_epoch, t_valid-t_train]
        results += [curr_result]
        if args.validate_src:
            f1_valid = f1_src
        else:
            f1_valid = f1_tgt
        if f1_valid>bestf1:
            save_model(args, encoder, 'mmdbestmodel' +param.src_encoder_path)
            save_model(args, classifier, 'mmdbestmodel' +param.src_classifier_path)
            bestf1 = f1_valid
            best_epoch = curr_result
    results+=[best_epoch]
    if not args.last_epoch:
        encoder = init_model(args, encoder, restore='mmdbestmodel' +param.src_encoder_path)
        classifier = init_model(args, classifier, restore='mmdbestmodel' +param.src_classifier_path)
    return encoder,classifier,results

def evaluate(args, encoder, classifier, data_loader, src_data_loader, flag=None,epoch=None,pattern=None):
    # set eval state for Dropout and BN layers
    encoder.eval()
    classifier.eval()

    # init loss and accuracy
    loss = 0
    acc = 0
    tp = 0
    fp = 0
    p = 0

    # set loss function
    criterion = nn.CrossEntropyLoss()

    # evaluate network
    first = 0
    l_ids, r_ids, labels_list, predictions = [], [], [], []
    for (reviews, mask,segment, labels,_,l_id,r_id) in data_loader:
        
        truelen = torch.sum(mask, dim=1)
        reviews = make_cuda(reviews)
        mask = make_cuda(mask)
        segment = make_cuda(segment)
        labels = make_cuda(labels)
        
        with torch.no_grad():
            feat = encoder(reviews, mask,segment)    
            preds = classifier(feat)
        loss += criterion(preds, labels).item()
        # print(preds)
        pred_cls = preds.data.max(1)[1]
        
        acc += pred_cls.eq(labels.data).cpu().sum().item()
        for i in range(len(labels)):
            if labels[i] == 1:
                p += 1
                if pred_cls[i] == 1:
                    tp += 1
            else:
                if pred_cls[i] == 1:
                    fp += 1
        labels_list += list(labels.detach().cpu().numpy())
        predictions+= list(preds.detach().cpu().numpy())
        l_ids += list(l_id.numpy())
        r_ids += list(r_id.numpy())
    div_safe = 0.000001
    print("p",p)
    print("tp",tp)
    print("fp",fp)
    recall = tp/(p+div_safe)
    
    precision = tp/(tp+fp+div_safe)
    f1 = 2*recall*precision/(recall + precision + div_safe)
    print("recall",recall)
    print("precision",precision)
    print("f1",f1)

    loss /= len(data_loader)
    acc /= len(data_loader.dataset)

    print("Avg Loss = %.4f, Avg Accuracy = %.4f" % (loss, acc))

    if flag:
            f = open('res.csv','a',encoding='utf-8',newline="")
            csv_writer = csv.writer(f)
            row = []
            row.append([flag,p,tp,fp,recall,precision,f1])
            csv_writer.writerows(row)
            f.close()

    return f1,loss, labels_list, predictions, l_ids, r_ids
