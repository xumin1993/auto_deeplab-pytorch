import torch.nn as nn
import torch
import torch.nn.functional as F


def fixed_padding(inputs, kernel_size, dilation):
    """
    https://github.com/jfzhang95/pytorch-deeplab-xception/blob/master/modeling/backbone/xception.py
    :param kernel_size:
    :param dilation:
    :return:
    """
    kernel_size_effective = kernel_size + (kernel_size - 1) * (dilation - 1)
    pad_total = kernel_size_effective - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg
    padded_inputs = F.pad(inputs, [pad_beg, pad_end, pad_beg, pad_end])
    return padded_inputs


class SeparableConv2d(nn.Module):
    def __init__(self, inplanes, planes, kernel_size=3, stride=1, dilation=1, bias=False, batch_norm=None):
        super(SeparableConv2d, self).__init__()

        self.conv1 = nn.Conv2d(inplanes, inplanes, kernel_size, stride, 0, dilation, groups=inplanes, bias=bias)
        self.bn = batch_norm(inplanes)
        self.pointwise = nn.Conv2d(inplanes, planes, 1, 1, 0, 1, 1, bias=bias)

    def forward(self, x):
        x = fixed_padding(x, self.conv1.kernel_size[0], dilation=self.conv1.dilation[0])
        x = self.conv1(x)
        if self.bn is not None:
            x = self.bn(x)
        x = self.pointwise(x)
        return x


class Cell(nn.Module):
    def __init__(self, in_channels_h1, in_channels_h2, out_channels, dilation=1, activation=nn.ReLU6, bn=nn.BatchNorm2d):
        """
        Initialization of inverted residual block
        :param in_channels_h1: number of input channels in h-1
        :param in_channels_h2: number of input channels in h-2
        :param out_channels: number of output channels
        :param t: the expansion factor of block
        :param s: stride of the first convolution
        :param dilation: dilation rate of 3*3 depthwise conv
        """
        super(Cell, self).__init__()
        self.in_ = in_channels_h1
        self.out_ = out_channels
        self.activation = activation

        if in_channels_h1 != in_channels_h2:
            self.reduce = FactorizedReduce(in_channels_h2, in_channels_h1)

        self.atr3x3 = nn.Sequential(
            nn.Conv2d(in_channels_h1, out_channels, kernel_size=(3, 3), dilation=dilation, bias=False),
            nn.BatchNorm2d(out_channels),
            self.activation(),
        )
        self.atr5x5 = nn.Sequential(
            nn.Conv2d(in_channels_h1, out_channels, kernel_size=(5, 5), dilation=dilation, bias=False),
            nn.BatchNorm2d(out_channels),
            self.activation(),
        )

        self.sep3x3 = nn.Sequential(SeparableConv2d(in_channels_h1, out_channels, kernel_size=3, batch_norm=bn),
                                    activation())
        self.sep5x5 = nn.Sequential(SeparableConv2d(in_channels_h1, out_channels, kernel_size=5, batch_norm=bn),
                                    activation())

    def forward(self, h_1, h_2):
        """

        :param h_1:
        :param h_2:
        :return:
        """

        if self.reduce is not None:
            h_2 = self.reduce(h_2)

        top = self.atr5x5(h_2) + self.sep3x3(h_1)
        bottom = self.atr3x3(h_1) + self.sep3x3(h_2)
        middle = self.sep3x3(bottom) + self.sep3x3(h_2)

        top2 = self.sep5x5(top) + self.sep5x5(middle)
        bottom2 = self.atr5x5(top2) + self.sep5x5(bottom)

        concat = torch.cat([top, top2, middle, bottom2, bottom])

        return concat


class ASPP(nn.Module):
    def __init__(self, in_channels, out_channels, paddings, dilations):
        #todo depthwise separable conv
        super(ASPP, self).__init__()
        self.conv11 = nn.Sequential(nn.Conv2d(in_channels, out_channels, 1, bias=False,),
                                     nn.BatchNorm2d(256))
        self.conv33_1 = nn.Sequential(nn.Conv2d(in_channels, out_channels, 3,
                                                padding=paddings[0], dilation=dilations[0], bias=False,),
                                      nn.BatchNorm2d(256))
        self.conv33_2 = nn.Sequential(nn.Conv2d(in_channels, out_channels, 3,
                                                padding=paddings[1], dilation=dilations[1], bias=False,),
                                      nn.BatchNorm2d(256))
        self.conv33_3 = nn.Sequential(nn.Conv2d(in_channels, out_channels, 3,
                                                padding=paddings[2], dilation=dilations[2], bias=False,),
                                      nn.BatchNorm2d(256))
        self.concate_conv = nn.Sequential(nn.Conv2d(out_channels*5, out_channels, 1, bias=False),
                                      nn.BatchNorm2d(256))
        # self.upsample = nn.Upsample(mode='bilinear', align_corners=True)
    def forward(self, x):
        conv11 = self.conv11(x)
        conv33_1 = self.conv33_1(x)
        conv33_2 = self.conv33_2(x)
        conv33_3 = self.conv33_3(x)

        # image pool and upsample
        image_pool = nn.AvgPool2d(kernel_size=x.size()[2:])
        image_pool = image_pool(x)
        image_pool = self.conv11(image_pool)
        upsample = nn.Upsample(size=x.size()[2:], mode='bilinear', align_corners=True)
        upsample = upsample(image_pool)

        # concate
        concate = torch.cat([conv11, conv33_1, conv33_2, conv33_3, upsample], dim=1)


# Based on quark0/darts on github
class FactorizedReduce(nn.Module):

  def __init__(self, C_in, C_out, affine=True):
    super(FactorizedReduce, self).__init__()
    assert C_out % 2 == 0
    self.relu = nn.ReLU(inplace=False)
    self.conv_1 = nn.Conv2d(C_in, C_out // 2, 1, stride=2, padding=0, bias=False)
    self.conv_2 = nn.Conv2d(C_in, C_out // 2, 1, stride=2, padding=0, bias=False)
    self.bn = nn.BatchNorm2d(C_out, affine=affine)

  def forward(self, x):
    x = self.relu(x)
    padded = F.pad(x, (0, 1, 0, 1), "constant", 0)
    path2 = self.conv_2(padded[:, :, 1:, 1:])
    out = torch.cat([self.conv_1(x), path2], dim=1)
    out = self.bn(out)
    return out
