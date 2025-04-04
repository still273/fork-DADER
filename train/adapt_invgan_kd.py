import torch
import torch.nn.functional as F
import torch.nn as nn
import torch.optim as optim
from train.evaluate import evaluate
import param
from utils import make_cuda,save_model,init_model
import csv
import os
import math
import datetime
import time

def adapt(args, src_encoder, tgt_encoder, discriminator,
          src_classifier, src_data_loader, tgt_data_train_loader, tgt_data_all_loader):
    """INvGAN+KD without valid data"""

    # set train state for Dropout and BN layers
    src_encoder.eval()
    src_classifier.eval()
    tgt_encoder.train()
    discriminator.train()

    # setup criterion and optimizer
    BCELoss = nn.BCELoss()
    KLDivLoss = nn.KLDivLoss(reduction='batchmean')

    optimizer_G = optim.Adam(tgt_encoder.parameters(), lr=args.d_learning_rate)
    optimizer_D = optim.Adam(discriminator.parameters(), lr=args.d_learning_rate)
    len_data_loader = min(len(src_data_loader), len(tgt_data_train_loader))
    start = datetime.datetime.now()
    for epoch in range(args.num_epochs):
        # zip source and target data pair
        data_zip = enumerate(zip(src_data_loader, tgt_data_train_loader))
        for step, ((reviews_src, src_mask,src_segment, _, _, _), (reviews_tgt, tgt_mask,tgt_segment, _, _, _)) in data_zip:
            reviews_src = make_cuda(reviews_src)
            src_mask = make_cuda(src_mask)
            src_segment = make_cuda(src_segment)

            reviews_tgt = make_cuda(reviews_tgt)
            tgt_mask = make_cuda(tgt_mask)
            tgt_segment = make_cuda(tgt_segment)

            # zero gradients for optimizer
            optimizer_D.zero_grad()

            # extract and concat features
            with torch.no_grad():
                feat_src = src_encoder(reviews_src, src_mask,src_segment)
            feat_src_tgt = tgt_encoder(reviews_src, src_mask,src_segment)
            feat_tgt = tgt_encoder(reviews_tgt, tgt_mask,tgt_segment)
            feat_concat = torch.cat((feat_src_tgt, feat_tgt), 0)

            # predict on discriminator
            pred_concat = discriminator(feat_concat.detach())

            # prepare real and fake label
            label_src = make_cuda(torch.ones(feat_src_tgt.size(0))).unsqueeze(1)
            label_tgt = make_cuda(torch.zeros(feat_tgt.size(0))).unsqueeze(1)
            label_concat = torch.cat((label_src, label_tgt), 0)

            # compute loss for discriminator
            dis_loss = BCELoss(pred_concat, label_concat)
            dis_loss.backward()

            for p in discriminator.parameters():
                p.data.clamp_(-args.clip_value, args.clip_value)
            # optimize discriminator
            optimizer_D.step()

            pred_cls = torch.squeeze(pred_concat.max(1)[1])
            acc = (pred_cls == label_concat).float().mean()

            # zero gradients for optimizer
            optimizer_G.zero_grad()
            T = args.temperature

            # predict on discriminator
            pred_tgt = discriminator(feat_tgt)

            # logits for KL-divergence
            with torch.no_grad():
                src_prob = F.softmax(src_classifier(feat_src) / T, dim=-1)
            tgt_prob = F.log_softmax(src_classifier(feat_src_tgt) / T, dim=-1)
            kd_loss = KLDivLoss(tgt_prob, src_prob.detach()) * T * T

            # compute loss for target encoder
            gen_loss = BCELoss(pred_tgt, label_src)
            loss_tgt = args.alpha * gen_loss + args.beta * kd_loss
            loss_tgt.backward()
            torch.nn.utils.clip_grad_norm_(tgt_encoder.parameters(), args.max_grad_norm)
            # optimize target encoder
            optimizer_G.step()

            if (step + 1) % args.log_step == 0:
                print("Epoch [%.2d/%.2d] Step [%.3d/%.3d]: "
                      "acc=%.4f g_loss=%.4f d_loss=%.4f kd_loss=%.4f"
                      % (epoch + 1,
                         args.num_epochs,
                         step + 1,
                         len_data_loader,
                         acc.item(),
                         gen_loss.item(),
                         dis_loss.item(),
                         kd_loss.item()))

        if args.rec_epoch:
            end = datetime.datetime.now()
            now_time = end-start
            res = evaluate(tgt_encoder, src_classifier, tgt_data_all_loader)
            f = open(args.epoch_path+args.src+args.srcfix+'-'+args.tgt+args.tgtfix+'.csv','a+',encoding='utf-8',newline="")
            csv_writer = csv.writer(f)
            row = [epoch+1,res,now_time]
            csv_writer.writerow(row)
            f.close()

    return tgt_encoder,discriminator

def adapt_best(args, src_encoder, tgt_encoder, discriminator,
          src_classifier, src_data_loader, src_valid_loader, tgt_data_train_loader, tgt_data_valid_loader):
    """INvGAN+KD with valid data"""

    # set train state for Dropout and BN layers
    src_encoder.eval()
    src_classifier.eval()
    tgt_encoder.train()
    discriminator.train()

    # setup criterion and optimizer
    BCELoss = nn.BCELoss()
    KLDivLoss = nn.KLDivLoss(reduction='batchmean')

    optimizer_G = optim.Adam(tgt_encoder.parameters(), lr=args.d_learning_rate)
    optimizer_D = optim.Adam(discriminator.parameters(), lr=args.d_learning_rate)
    len_data_loader = min(len(src_data_loader), len(tgt_data_train_loader))
    start = datetime.datetime.now()

    best_f1 = 0
    res = -1.0
    tgt_res = -1.0

    results = []
    best_epoch = []
    for epoch in range(args.num_epochs):
        t_epoch = time.perf_counter()
        # zip source and target data pair
        data_zip = enumerate(zip(src_data_loader, tgt_data_train_loader))
        for step, ((reviews_src, src_mask,src_segment, _,_, _, _), (reviews_tgt, tgt_mask,tgt_segment, _,_, _, _)) in data_zip:
            reviews_src = make_cuda(reviews_src)
            src_mask = make_cuda(src_mask)
            src_segment = make_cuda(src_segment)

            reviews_tgt = make_cuda(reviews_tgt)
            tgt_mask = make_cuda(tgt_mask)
            tgt_segment = make_cuda(tgt_segment)

            # zero gradients for optimizer
            optimizer_D.zero_grad()

            # extract and concat features
            with torch.no_grad():
                feat_src = src_encoder(reviews_src, src_mask,src_segment)
            feat_src_tgt = tgt_encoder(reviews_src, src_mask,src_segment)
            feat_tgt = tgt_encoder(reviews_tgt, tgt_mask,tgt_segment)
            feat_concat = torch.cat((feat_src_tgt, feat_tgt), 0)

            # predict on discriminator
            pred_concat = discriminator(feat_concat.detach())

            # prepare real and fake label
            label_src = make_cuda(torch.ones(feat_src_tgt.size(0))).unsqueeze(1)
            label_tgt = make_cuda(torch.zeros(feat_tgt.size(0))).unsqueeze(1)
            label_concat = torch.cat((label_src, label_tgt), 0)

            # compute loss for discriminator
            dis_loss = BCELoss(pred_concat, label_concat)
            dis_loss.backward()

            for p in discriminator.parameters():
                p.data.clamp_(-args.clip_value, args.clip_value)
            # optimize discriminator
            optimizer_D.step()

            pred_cls = torch.squeeze(pred_concat.max(1)[1])
            acc = (pred_cls == label_concat).float().mean()

            # zero gradients for optimizer
            optimizer_G.zero_grad()
            T = args.temperature

            # predict on discriminator
            pred_tgt = discriminator(feat_tgt)

            # logits for KL-divergence
            with torch.no_grad():
                src_prob = F.softmax(src_classifier(feat_src) / T, dim=-1)
            tgt_prob = F.log_softmax(src_classifier(feat_src_tgt) / T, dim=-1)
            kd_loss = KLDivLoss(tgt_prob, src_prob.detach()) * T * T

            # compute loss for target encoder
            gen_loss = BCELoss(pred_tgt, label_src)
            loss_tgt = args.alpha * gen_loss + args.beta * kd_loss
            loss_tgt.backward()
            torch.nn.utils.clip_grad_norm_(tgt_encoder.parameters(), args.max_grad_norm)
            # optimize target encoder
            optimizer_G.step()

            if (step + 1) % args.log_step == 0:
                print("Epoch [%.2d/%.2d] Step [%.3d/%.3d]: "
                      "acc=%.4f g_loss=%.4f d_loss=%.4f kd_loss=%.4f"
                      % (epoch + 1,
                         args.num_epochs,
                         step + 1,
                         len_data_loader,
                         acc.item(),
                         gen_loss.item(),
                         dis_loss.item(),
                         kd_loss.item()))
        
        if args.rec_epoch:
            end = datetime.datetime.now()
            now_time = end-start
            res = evaluate(tgt_encoder, src_classifier, tgt_data_train_loader)
            f = open(args.epoch_path+args.src+args.srcfix+'-'+args.tgt+args.tgtfix+str(args.train_seed)+args.rec_lr+'.csv','a+',encoding='utf-8',newline="")
            csv_writer = csv.writer(f)
            row = [epoch,res,now_time]
            csv_writer.writerow(row)
            f.close()
        t_train = time.perf_counter()
        src_res = evaluate(tgt_encoder, src_classifier, src_valid_loader)
        tgt_res = evaluate(tgt_encoder, src_classifier, tgt_data_valid_loader)
        t_valid = time.perf_counter()

        curr_res = [epoch, src_res, tgt_res,  t_train-t_epoch, t_valid-t_train]
        results += [curr_res]
        if args.validate_src:
            f1 = src_res
        else:
            f1 = tgt_res
        if f1 > best_f1:
            print("best epoch: ",epoch)
            print("F1: ",f1)
            best_f1 = f1
            if args.need_kd_model:
                save_model(args, tgt_encoder, 'best' + param.tgt_encoder_path)

            print("======== tgt result =======")
            if args.rec_epoch:
                tgt_res = res
            else:
                tgt_res = evaluate(tgt_encoder, src_classifier, tgt_data_train_loader)
            best_epoch = curr_res
    results += [best_epoch]
    if not args.last_epoch:
        tgt_encoder = init_model(args, tgt_encoder, restore='best'+param.tgt_encoder_path)
    return tgt_encoder,discriminator,tgt_res,best_f1, results

def adapt_best_semi(args, src_encoder, tgt_encoder, discriminator,
          src_classifier, src_data_loader, tgt_data_train_loader, tgt_data_valid_loader,tgt_data_test_loader=None):
    """INvGAN+KD with valid data for semi-supervised learning"""

    # set train state for Dropout and BN layers
    src_encoder.eval()
    src_classifier.eval()
    tgt_encoder.train()
    discriminator.train()

    # setup criterion and optimizer
    BCELoss = nn.BCELoss()
    KLDivLoss = nn.KLDivLoss(reduction='batchmean')

    optimizer_G = optim.Adam(tgt_encoder.parameters(), lr=args.d_learning_rate)
    optimizer_D = optim.Adam(discriminator.parameters(), lr=args.d_learning_rate)
    len_data_loader = min(len(src_data_loader), len(tgt_data_train_loader))
    start = datetime.datetime.now()

    best_f1 = 0
    res = -1.0
    tgt_res = -1.0
    for epoch in range(args.num_epochs):
        # zip source and target data pair
        data_zip = enumerate(zip(src_data_loader, tgt_data_train_loader))
        for step, ((reviews_src, src_mask,src_segment, _,_), (reviews_tgt, tgt_mask,tgt_segment, _,_)) in data_zip:
            reviews_src = make_cuda(reviews_src)
            src_mask = make_cuda(src_mask)
            src_segment = make_cuda(src_segment)

            reviews_tgt = make_cuda(reviews_tgt)
            tgt_mask = make_cuda(tgt_mask)
            tgt_segment = make_cuda(tgt_segment)

            # zero gradients for optimizer
            optimizer_D.zero_grad()

            # extract and concat features
            with torch.no_grad():
                feat_src = src_encoder(reviews_src, src_mask,src_segment)
            feat_src_tgt = tgt_encoder(reviews_src, src_mask,src_segment)
            feat_tgt = tgt_encoder(reviews_tgt, tgt_mask,tgt_segment)
            feat_concat = torch.cat((feat_src_tgt, feat_tgt), 0)

            # predict on discriminator
            pred_concat = discriminator(feat_concat.detach())

            # prepare real and fake label
            label_src = make_cuda(torch.ones(feat_src_tgt.size(0))).unsqueeze(1)
            label_tgt = make_cuda(torch.zeros(feat_tgt.size(0))).unsqueeze(1)
            label_concat = torch.cat((label_src, label_tgt), 0)

            # compute loss for discriminator
            dis_loss = BCELoss(pred_concat, label_concat)
            dis_loss.backward()

            for p in discriminator.parameters():
                p.data.clamp_(-args.clip_value, args.clip_value)
            # optimize discriminator
            optimizer_D.step()

            pred_cls = torch.squeeze(pred_concat.max(1)[1])
            acc = (pred_cls == label_concat).float().mean()

            # zero gradients for optimizer
            optimizer_G.zero_grad()
            T = args.temperature

            # predict on discriminator
            pred_tgt = discriminator(feat_tgt)

            # logits for KL-divergence
            with torch.no_grad():
                src_prob = F.softmax(src_classifier(feat_src) / T, dim=-1)
            tgt_prob = F.log_softmax(src_classifier(feat_src_tgt) / T, dim=-1)
            kd_loss = KLDivLoss(tgt_prob, src_prob.detach()) * T * T

            # compute loss for target encoder
            gen_loss = BCELoss(pred_tgt, label_src)
            loss_tgt = args.alpha * gen_loss + args.beta * kd_loss
            loss_tgt.backward()
            torch.nn.utils.clip_grad_norm_(tgt_encoder.parameters(), args.max_grad_norm)
            # optimize target encoder
            optimizer_G.step()

            if (step + 1) % args.log_step == 0:
                print("Epoch [%.2d/%.2d] Step [%.3d/%.3d]: "
                      "acc=%.4f g_loss=%.4f d_loss=%.4f kd_loss=%.4f"
                      % (epoch + 1,
                         args.num_epochs,
                         step + 1,
                         len_data_loader,
                         acc.item(),
                         gen_loss.item(),
                         dis_loss.item(),
                         kd_loss.item()))
        
        f1 = evaluate(tgt_encoder, src_classifier, tgt_data_valid_loader)
        if f1 > best_f1:
            print("best epoch: ",epoch)
            print("F1: ",f1)
            best_f1 = f1
            if tgt_data_test_loader:
                print("=== tgt ===")
                print(evaluate(tgt_encoder, src_classifier, tgt_data_test_loader))
            if args.need_kd_model:
                save_model(args, tgt_encoder, param.tgt_encoder_path+args.tgt+'best_semi')

    tgt_encoder = init_model(args, tgt_encoder, restore=param.tgt_encoder_path+args.tgt+'best_semi')
    return tgt_encoder,discriminator
