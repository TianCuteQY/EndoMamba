import numpy as np
from PIL import Image
import random
import torch
from torch.utils.data import Dataset
import torchvision.transforms as transforms
import cv2
import os
import imageio


class CenterCrop(object):
    def __init__(self, arg):
        self.transform = transforms.CenterCrop(arg)

    def __call__(self, sample):
        img, label = sample
        return self.transform(img), self.transform(label)


class Resize(object):
    def __init__(self, arg):
        self.transform_img = transforms.Resize(arg, Image.BILINEAR)
        self.transform_label = transforms.Resize(arg, Image.NEAREST)

    def __call__(self, sample):
        img, label = sample
        return self.transform_img(img), self.transform_label(label)


class Normalize(object):
    def __init__(self, mean, std):
        self.transform = transforms.Normalize(mean, std)

    def __call__(self, sample):
        img, label = sample
        return self.transform(img), label


class ToTensor(object):
    def __init__(self):
        pass

    def __call__(self, sample):
        img, label = sample
        label = np.array(label)  # / 255
        img = np.array(img)

        # img[img > 150] = 150
        # img[img < -1350] = -1350
        img = (img - img.min()) / (img.max() - img.min())
        return torch.from_numpy(img.transpose((2, 0, 1))).float(), torch.from_numpy(label.copy()).long()


class RandomRescale(object):
    def __init__(self, min_ratio=0.5, max_ratio=1.0):
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio

    def __call__(self, sample):
        img, label = sample
        width, height = img.size
        ratio = random.uniform(self.min_ratio, self.max_ratio)
        new_width, new_height = int(ratio * width), int(ratio * height)
        return img.resize((new_width, new_height)), label.resize((new_width, new_height))


class RandomFlip(object):
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, sample):
        img, label = sample
        if random.uniform(0, 1) > self.p:
            return transforms.functional.hflip(img), transforms.functional.hflip(label)
        else:
            return img, label


class RandomColor(object):
    def __init__(self, brightness=0, contrast=0.2, saturation=0, hue=0):
        self.transform = transforms.ColorJitter(brightness, contrast, saturation, hue)

    def __call__(self, sample):
        img, label = sample
        return self.transform(img), label


class RandomRotation(object):
    def __init__(self, degree=[-5, 5]):
        self.degree = degree

    def __call__(self, sample):
        img, label = sample

        angle = transforms.RandomRotation.get_params(self.degree)

        img = transforms.functional.rotate(img, angle)
        label = transforms.functional.rotate(label, angle)
        return img, label


class RandomCrop(object):
    def __init__(self, output_size):
        self.output_size = output_size

    def __call__(self, sample):
        img, label = sample

        i, j, h, w = transforms.RandomCrop.get_params(
            img, output_size=self.output_size)

        img = transforms.functional.crop(img, i, j, h, w)
        label = transforms.functional.crop(label, i, j, h, w)
        return img, label


def read_txt(file):
    tmp = []
    with open(file, "r") as f:
        for line in f.readlines():
            line = line.strip('\n')
            tmp.append(line)

    return tmp


class SegCTDataset(Dataset):
    """Covid XRay dataset."""

    def __init__(self, dataroot, transforms, mode='train'):
        self.dataroot = dataroot
        self.mode = mode
        self.IMAGE_LIB = os.path.join(dataroot, 'CVC-ClinicDB/Original/')
        self.MASK_LIB = os.path.join(dataroot, 'CVC-ClinicDB/Ground Truth/')

        if mode == 'train':
            self.videos = ['CVC-ClinicVideoDB/indexed 104 23 -1', 'CVC-ClinicVideoDB/indexed 529 18 -1', 'CVC-ClinicVideoDB/indexed 448 19 -1', 'CVC-ClinicVideoDB/indexed 26 25 -1', 'CVC-ClinicVideoDB/indexed 343 21 -1', 'CVC-ClinicVideoDB/indexed 178 22 -1', 'CVC-ClinicVideoDB/indexed 429 19 -1', 'CVC-ClinicVideoDB/indexed 200 6 -1', 'CVC-ClinicVideoDB/indexed 127 25 -1', 'CVC-ClinicVideoDB/indexed 253 25 -1', 'CVC-ClinicVideoDB/indexed 364 20 -1', 'CVC-ClinicVideoDB/indexed 592 21 -1', 'CVC-ClinicVideoDB/indexed 228 25 -1', 'CVC-ClinicVideoDB/indexed 68 11 -1', 'CVC-ClinicVideoDB/indexed 467 12 -1', 'CVC-ClinicVideoDB/indexed 206 22 -1', 'CVC-ClinicVideoDB/indexed 79 25 -1', 'CVC-ClinicVideoDB/indexed 479 25 -1','CVC-ClinicVideoDB/indexed 51 17 -1', 'CVC-ClinicVideoDB/indexed 572 20 -1']
        else:
            self.videos = ['CVC-ClinicVideoDB/indexed 51 17 -1', 'CVC-ClinicVideoDB/indexed 318 25 -1', 'CVC-ClinicVideoDB/indexed 429 19 -1', 'CVC-ClinicVideoDB/indexed 1 25 -1', 'CVC-ClinicVideoDB/indexed 178 22 -1', 'CVC-ClinicVideoDB/indexed 253 25 -1', 'CVC-ClinicVideoDB/indexed 343 21 -1', 'CVC-ClinicVideoDB/indexed 68 11 -1', 'CVC-ClinicVideoDB/indexed 200 6 -1']

        self.transform = transforms

    def __len__(self):
        return len(self.videos)

    def __getitem__(self, idx):
        video_info = self.videos[idx].split(' ')
        start_idx = int(video_info[1])
        end_idx = start_idx + int(video_info[2])
        # print(video_info)
        # exit(0)

        # if self.mode in ['train']:
        #     frame_number = 10
        #     if int(video_info[2]) > frame_number:
        #         start_index = np.random.randint(start_idx, end_idx - frame_number)
        #         end_idx = start_idx + frame_number

        all_img = []
        all_label = []

        for i in range(start_idx, end_idx):
            image_name = f'{i}.tif'

            img = imageio.imread(self.IMAGE_LIB + image_name)
            img = np.array(img).astype('float32')
            # img = cv2.imread(self.IMAGE_LIB + image_name, cv2.IMREAD_UNCHANGED).astype("int16").astype('float32')
            img = (img - np.min(img)) / (np.max(img) - np.min(img))
            # print(img.shape)
            img = Image.fromarray(np.uint8(img * 255)).convert('RGB')
            # img = Image.open(self.IMAGE_LIB + image_name)
            label = Image.open(self.MASK_LIB + image_name)

            # print(np.sum(label), np.max(label), np.min(label))

            if self.transform:
                img, label = self.transform((img, label))

            # print(label.shape)
            # print(label.shape, torch.unique(label))
            # exit(0)

            label[label < 255] = 0
            label[label == 255] = 1

            all_img.append(img.unsqueeze(0))
            all_label.append(label.unsqueeze(0))

            # print(img.shape, label.shape)
            # print(torch.max(label), torch.min(label), torch.sum(label))
            # exit(0)

        # if self.mode in ['train']:
        #     sample = {'image': torch.cat(all_img), 'label': torch.cat(all_label)}
        # else:
        #     sample = {'image': torch.cat(all_img), 'label': torch.cat(all_label), 'case_name': '0'}

        sample = {'image': torch.cat(all_img), 'label': torch.cat(all_label)}

        # print(set(list(label.numpy().flatten())))
        # exit(0)

        return sample


# def convertlabel(ori, num=5):
#     if num == 5:
#         pro = torch.zeros(ori.shape)
#
#         pro[ori == 1] = 1
#         pro[ori == 7] = 1
#
#         pro[ori == 6] = 2
#         pro[ori == 5] = 2
#         pro[ori == 15] = 2
#
#         pro[ori == 8] = 3
#         pro[ori == 9] = 3
#         pro[ori == 10] = 3
#         pro[ori == 4] = 3
#
#         pro[ori == 3] = 4
#         pro[ori == 2] = 4
#         pro[ori == 14] = 4
#
#         pro[ori == 11] = 5
#         pro[ori == 12] = 5
#         pro[ori == 13] = 5
#
#     return pro


if __name__ == '__main__':
    from datasets.dataset_synapse import dynamic_padding_collate_fn
    from torch.utils.data import DataLoader

    test_transform = transforms.Compose([
        Resize((224, 224)),
        ToTensor(),
        Normalize(mean=[0.5, 0.5, 0.5],
                  std=[0.5, 0.5, 0.5])
    ])
    dataset = SegCTDataset(
        dataroot="/mnt/tqy/CVC-ClinicVideoDB/CVC-ClinicVideoDB/",
        transforms=test_transform,
        )
    
    dataloader = DataLoader(
    dataset,
    batch_size=16,  # 你可以自由调整 batch_size
    shuffle=True,
    collate_fn=dynamic_padding_collate_fn  # 使用前面定义的 dynamic_padding_collate_fn
    )

    # 测试 DataLoader
    for batch in dataloader:
        print("Batch image shape:", batch['image'].shape)  # 动态 padding 后的形状
        print("Batch label shape:", batch['label'].shape)
        break









