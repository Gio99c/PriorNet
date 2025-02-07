import sys

from matplotlib.pyplot import axis
sys.path.insert(1, "./")

from tkinter import Image
import torch
from torch import nn

from model.build_contextpath import build_contextpath
import warnings
warnings.filterwarnings(action='ignore')



class ConvBlock(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=2, padding=1):
         super().__init__()
         self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=padding, bias=False)
         self.bn = nn.BatchNorm2d(out_channels)
         self.relu = nn.ReLU()


    def forward(self, input):
        x = self.conv1(input)
        x = self.bn(x)
        x = self.relu(x)
        return x



class Spatial_path(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.convblock1 = ConvBlock(in_channels=3, out_channels=64)
        self.convblock2 = ConvBlock(in_channels=64, out_channels=128)
        self.convblock3 = ConvBlock(in_channels=128, out_channels=256)


    def forward(self, input):
        x = self.convblock1(input)
        x = self.convblock2(x)
        x = self.convblock3(x)
        return x




class AttentionRefinementModule(torch.nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.avgpool = nn.AdaptiveAvgPool2d(output_size=(1,1))
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        self.bn = nn.BatchNorm2d(out_channels)
        self.sigmoid = nn.Sigmoid()
        self.in_channels = in_channels
        

    def forward(self, input):
        # global average pooling
        x = self.avgpool(input)
        assert self.in_channels == x.size(1), f'in_channels and out_channels should all be {x.size(1)}'
        x = self.conv(x)
        x = self.bn(x)
        x = self.sigmoid(x)
        # channels of input and x should be same
        x = torch.mul(input, x)
        return x



class FeatureFusionModule(torch.nn.Module):
    def __init__(self, num_classes, in_channels):
        super().__init__()
        # self.in_channels = input_1.channels + input_2.channels
        # resnet101 3328 = 256(from spatial path) + 1024(from context path) + 2048(from context path)
        # resnet18  1024 = 256(from spatial path) + 256(from context path) + 512(from context path)
        self.in_channels = in_channels

        self.convblock = ConvBlock(in_channels=self.in_channels, out_channels=num_classes, stride=1)
        self.conv1 = nn.Conv2d(num_classes, num_classes, kernel_size=1)
        self.relu = nn.ReLU()
        self.conv2 = nn.Conv2d(num_classes, num_classes, kernel_size=1)
        self.sigmoid = nn.Sigmoid()
        self.avgpool = nn.AdaptiveAvgPool2d(output_size=(1,1))


    def forward(self, input1, input2):
        x = torch.cat((input1, input2), dim=1)
        assert self.in_channels == x.size(1), f'in_channels of ConvBlock should be {x.size(1)}'
        feature = self.convblock(x)

        x = self.avgpool(feature)
        x = self.conv1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x = self.sigmoid(x)

       

        x = torch.mul(feature, x)
        x = torch.add(x, feature)

        return x




class BiSeNet(torch.nn.Module):
    def __init__(self, num_classes, context_path):
        super().__init__()
        # build spatial path
        self.spatial_path = Spatial_path()

        # build context_path 
        self.context_path = build_contextpath(name=context_path)

        
        if context_path == 'resnet101':
            # build attention refinement module for resnet 101
            self.attention_refinement_module1 = AttentionRefinementModule(1024, 1024)
            self.attention_refinement_module2 = AttentionRefinementModule(2048, 2048)
            # supervision block
            self.supervision1 = nn.Conv2d(in_channels=1024, out_channels=num_classes, kernel_size=1)
            self.supervision2 = nn.Conv2d(in_channels=2048, out_channels=num_classes, kernel_size=1)
            # build feature fusion module
            self.feature_fusion_module = FeatureFusionModule(num_classes, 3328)

        elif context_path == 'resnet18':
            # build attention refinement module  for resnet 18
            self.attention_refinement_module1 = AttentionRefinementModule(256, 256)
            self.attention_refinement_module2 = AttentionRefinementModule(512, 512)
            # supervision block
            self.supervision1 = nn.Conv2d(in_channels=256, out_channels=num_classes, kernel_size=1)
            self.supervision2 = nn.Conv2d(in_channels=512, out_channels=num_classes, kernel_size=1)
            # build feature fusion module
            self.feature_fusion_module = FeatureFusionModule(num_classes, 1024)

        else:
            print(f"Error: {context_path} context_path network unsupported")

        # build final convolution
        self.conv = nn.Conv2d(in_channels=num_classes, out_channels=num_classes, kernel_size=1)

        self.init_weight()

        self.mul_lr = []
        self.mul_lr.append(self.spatial_path)
        self.mul_lr.append(self.attention_refinement_module1)
        self.mul_lr.append(self.attention_refinement_module2)
        self.mul_lr.append(self.supervision1)
        self.mul_lr.append(self.supervision2)
        self.mul_lr.append(self.feature_fusion_module)
        self.mul_lr.append(self.conv)


    def init_weight(self):
        for name, m in self.named_modules():
            if 'context_path' not in name:
                if isinstance(m, nn.Conv2d):
                     nn.init.kaiming_normal_(m.weight, mode='fan_in', nonlinearity='relu')
                elif isinstance(m, nn.BatchNorm2d):
                    m.eps = 1e-5
                    m.momentum = 0.1
                    nn.init.constant_(m.weight, 1)
                    nn.init.constant_(m.bias, 0)

    

    def forward(self, input):
        # output of spatial path
        sx = self.spatial_path(input)

        # output of context path
        cx1, cx2, tail = self.context_path(input)
        cx1 = self.attention_refinement_module1(cx1)
        cx2 = self.attention_refinement_module2(cx2)
        cx2 = torch.mul(cx2, tail)
        
        # upsampling
        cx1 = torch.nn.functional.interpolate(cx1, size=sx.size()[-2:], mode='bilinear')
        cx2 = torch.nn.functional.interpolate(cx2, size=sx.size()[-2:], mode='bilinear')
        cx = torch.cat((cx1, cx2), dim=1)

        if self.training == True:
            cx1_sup = self.supervision1(cx1)
            cx2_sup = self.supervision2(cx2)
            cx1_sup = torch.nn.functional.interpolate(cx1_sup, size=input.size()[-2:], mode='bilinear')
            cx2_sup = torch.nn.functional.interpolate(cx2_sup, size=input.size()[-2:], mode='bilinear')

        # output of feature fusion module
        result = self.feature_fusion_module(sx, cx)

        #qui result è descritto con probabilità
        # img = result[0]
        # print('Shape prob: ',img.shape)
        # print('Somma prob pointwise: ', torch.sum(result[0], axis = 1))
        # import numpy as np
        # print('Unique fusion: ',np.unique(img[0].detach().numpy()))
        # print(result)

        # upsampling
        result = torch.nn.functional.interpolate(result, scale_factor=8, mode='bilinear')

        result = self.conv(result)

        if self.training == True:
            return result, cx1_sup, cx2_sup

        return result



if __name__ == '__main__':
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    model = BiSeNet(19, 'resnet101')
    x = torch.rand(2, 3, 512, 1024)
    record = model.parameters()

    result, _, _ = model(x)
    result = result[0]
    print('Output finale:',result.shape)



    # model = nn.DataParallel(model)

    #model = model.cuda()
    
    # Imports PIL module 
    from PIL import Image
    from torchvision import transforms
    
    # open method used to open different extension image file
    # im = Image.open(r"data/Cityscapes/images/zurich_000075_000019_leftImg8bit.png") 
    # im = transforms.ToTensor()(im)
    # im2 = Image.open((r"data/Cityscapes/images/zurich_000116_000019_leftImg8bit.png"))
    # im2 = transforms.ToTensor()(im2)
    # print(im.shape)
    # x = torch.tensor([im.numpy(), im2.numpy()])
    # result = model(x)
    # print(result.shape)
    
    # This method will show image in any image viewer 
    #im.show() 

    print(model.parameters())