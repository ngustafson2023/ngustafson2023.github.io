"""This code is adapted from FreeSurfer mri_synthstrip.py to be compatible for Nobrainer-zoo.


If you use this code, please cite the SynthStrip paper:
SynthStrip: Skull-Stripping for Any Brain Image.
A Hoopes, JS Mora, AV Dalca, B Fischl, M Hoffmann.

https://github.com/freesurfer/freesurfer/blob/dev/mri_synthstrip/


Copyright 2022 A Hoopes

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at
http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software distributed under the License is
distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
"""

#!/usr/bin/env python

import os
import sys
import torch
import torch
import torch.nn as nn
import numpy as np
import argparse
import surfa as sf
import scipy.ndimage

description = '''
Robust, universal skull-stripping for brain images of any
type. If you use SynthStrip in your analysis, please cite:

SynthStrip: Skull-Stripping for Any Brain Image.
A Hoopes, JS Mora, AV Dalca, B Fischl, M Hoffmann.
'''

# parse command line
parser = argparse.ArgumentParser(description=description)
parser.add_argument('-i', '--image', metavar='file', required=True, help='Input image to skullstrip.')
parser.add_argument('-o', '--out', metavar='file', help='Save stripped image to path.')
parser.add_argument('-m', '--mask', metavar='file', help='Save binary brain mask to path.')
parser.add_argument('-g', '--gpu', action='store_true', help='Use the GPU.')
parser.add_argument('-b', '--border', default=1, type=int, help='Mask border threshold in mm. Default is 1.')
parser.add_argument('--model', metavar='file', help='Alternative model weights.')
if len(sys.argv) == 1:
    parser.print_help()
    exit(1)
args = parser.parse_args()

# sanity check on the inputs
if not args.out and not args.mask:
    sf.system.fatal('Must provide at least --out or --mask output flags.')

# necessary for speed gains (I think)
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True

# configure GPU device
if args.gpu:
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    device = torch.device('cuda')
    device_name = 'GPU'
else:
    os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    device = torch.device('cpu')
    device_name = 'CPU'

# configure model
print(f'Configuring model on the {device_name}')

class StripModel(nn.Module):

    def __init__(self,
                 nb_features=16,
                 nb_levels=7,
                 feat_mult=2,
                 max_features=64,
                 nb_conv_per_level=2,
                 max_pool=2,
                 return_mask=False):

        super().__init__()

        # dimensionality
        ndims = 3

        # build feature list automatically
        if isinstance(nb_features, int):
            if nb_levels is None:
                raise ValueError('must provide unet nb_levels if nb_features is an integer')
            feats = np.round(nb_features * feat_mult ** np.arange(nb_levels)).astype(int)
            feats = np.clip(feats, 1, max_features)
            nb_features = [
                np.repeat(feats[:-1], nb_conv_per_level),
                np.repeat(np.flip(feats), nb_conv_per_level)
            ]
        elif nb_levels is not None:
            raise ValueError('cannot use nb_levels if nb_features is not an integer')

        # extract any surplus (full resolution) decoder convolutions
        enc_nf, dec_nf = nb_features
        nb_dec_convs = len(enc_nf)
        final_convs = dec_nf[nb_dec_convs:]
        dec_nf = dec_nf[:nb_dec_convs]
        self.nb_levels = int(nb_dec_convs / nb_conv_per_level) + 1

        if isinstance(max_pool, int):
            max_pool = [max_pool] * self.nb_levels

        # cache downsampling / upsampling operations
        MaxPooling = getattr(nn, 'MaxPool%dd' % ndims)
        self.pooling = [MaxPooling(s) for s in max_pool]
        self.upsampling = [nn.Upsample(scale_factor=s, mode='nearest') for s in max_pool]

        # configure encoder (down-sampling path)
        prev_nf = 1
        encoder_nfs = [prev_nf]
        self.encoder = nn.ModuleList()
        for level in range(self.nb_levels - 1):
            convs = nn.ModuleList()
            for conv in range(nb_conv_per_level):
                nf = enc_nf[level * nb_conv_per_level + conv]
                convs.append(ConvBlock(ndims, prev_nf, nf))
                prev_nf = nf
            self.encoder.append(convs)
            encoder_nfs.append(prev_nf)

        # configure decoder (up-sampling path)
        encoder_nfs = np.flip(encoder_nfs)
        self.decoder = nn.ModuleList()
        for level in range(self.nb_levels - 1):
            convs = nn.ModuleList()
            for conv in range(nb_conv_per_level):
                nf = dec_nf[level * nb_conv_per_level + conv]
                convs.append(ConvBlock(ndims, prev_nf, nf))
                prev_nf = nf
            self.decoder.append(convs)
            if level < (self.nb_levels - 1):
                prev_nf += encoder_nfs[level]

        # now we take care of any remaining convolutions
        self.remaining = nn.ModuleList()
        for num, nf in enumerate(final_convs):
            self.remaining.append(ConvBlock(ndims, prev_nf, nf))
            prev_nf = nf

        # final convolutions
        if return_mask:
            self.remaining.append(ConvBlock(ndims, prev_nf, 2, activation=None))
            self.remaining.append(nn.Softmax(dim=1))
        else:
            self.remaining.append(ConvBlock(ndims, prev_nf, 1, activation=None))

    def forward(self, x):

        # encoder forward pass
        x_history = [x]
        for level, convs in enumerate(self.encoder):
            for conv in convs:
                x = conv(x)
            x_history.append(x)
            x = self.pooling[level](x)

        # decoder forward pass with upsampling and concatenation
        for level, convs in enumerate(self.decoder):
            for conv in convs:
                x = conv(x)
            if level < (self.nb_levels - 1):
                x = self.upsampling[level](x)
                x = torch.cat([x, x_history.pop()], dim=1)

        # remaining convs at full resolution
        for conv in self.remaining:
            x = conv(x)

        return x

class ConvBlock(nn.Module):
    """
    Specific convolutional block followed by leakyrelu for unet.
    """

    def __init__(self, ndims, in_channels, out_channels, stride=1, activation='leaky'):
        super().__init__()

        Conv = getattr(nn, 'Conv%dd' % ndims)
        self.conv = Conv(in_channels, out_channels, 3, stride, 1)
        if activation == 'leaky':
            self.activation = nn.LeakyReLU(0.2)
        elif activation == None:
            self.activation = None
        else:
            raise ValueError(f'Unknown activation: {activation}')

    def forward(self, x):
        out = self.conv(x)
        if self.activation is not None:
            out = self.activation(out)
        return out

with torch.no_grad():
    model = StripModel()
    model.to(device)
    model.eval()

# load model weights
if args.model is not None:
    modelfile = args.model
    print('Using custom model weights')
else:
    version = '1'
    print(f'Running SynthStrip model version {version}')
    fshome = os.environ.get('FREESURFER_HOME')
    if fshome is None:
        sf.system.fatal('FREESURFER_HOME env variable must be set! Make sure FreeSurfer is properly sourced.')
    modelfile = os.path.join(fshome, 'models', f'synthstrip.{version}.pt')
checkpoint = torch.load(modelfile, map_location=device)
model.load_state_dict(checkpoint['model_state_dict'])

# load input volume
image = sf.load_volume(args.image)
print(f'Input image read from: {args.image}')

# frame check
if image.nframes > 1:
    sf.system.fatal('Input image cannot have more than 1 frame')

# conform image and fit to shape with factors of 64
conformed = image.conform(voxsize=1.0, dtype='float32', method='nearest', orientation='LIA').crop_to_bbox()
target_shape = np.clip(np.ceil(np.array(conformed.shape[:3]) / 64).astype(int) * 64, 192, 320)
conformed = conformed.reshape(target_shape)

# normalize intensities
conformed -= conformed.min()
conformed = (conformed / conformed.percentile(99)).clip(0, 1)

# predict the surface distance transform
with torch.no_grad():
    input_tensor = torch.from_numpy(conformed.data[np.newaxis, np.newaxis]).to(device)
    sdt = model(input_tensor).cpu().numpy().squeeze()

# unconform the sdt and extract mask
sdt = conformed.new(sdt).resample_like(image, fill=100)

# find largest CC (just do this to be safe for now)
components = scipy.ndimage.label(sdt.data < args.border)[0]
bincount = np.bincount(components.flatten())[1:]
mask = (components == (np.argmax(bincount) + 1))
mask = scipy.ndimage.binary_fill_holes(mask)

# write the masked output
if args.out:
    image[mask == 0] = np.min([0, image.min()])
    image.save(args.out)
    print(f'Masked image saved to: {args.out}')

# write the brain mask
if args.mask:
    image.new(mask).save(args.mask)
    print(f'Binary brain mask saved to: {args.mask}')

print('If you use SynthStrip in your analysis, please cite:')
print('----------------------------------------------------')
print('SynthStrip: Skull-Stripping for Any Brain Image.')
print('A Hoopes, JS Mora, AV Dalca, B Fischl, M Hoffmann.')
