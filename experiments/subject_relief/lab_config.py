"""One validated parameter contract shared by the local lab API and page."""
from __future__ import annotations

import copy
import math


GROUPS = [
    {'id': 'depth', 'label': '深度推理', 'description': '决定模型看到的细节与计算预算。修改这些参数会计算新的深度。'},
    {'id': 'fusion', 'label': '多次推理与融合', 'description': '用不同输入观察同一画面；并非重复相同计算。'},
    {'id': 'mask', 'label': '保留区域与背景', 'description': '只决定哪里保留起伏，不把颜色变成不同高度。'},
    {'id': 'shape', 'label': '浮雕形状', 'description': '使用已有深度重建真实模型，通常可复用推理结果，运行最快。'},
    {'id': 'advanced', 'label': '高级边界与资源', 'description': '可手动调整处理分辨率、修边和深度范围。', 'advanced': True},
]


def number(key, label, default, minimum, maximum, step, group, help_text, effect, *, integer=False, unit='', display_scale=1):
    return {'key': key, 'label': label, 'kind': 'integer' if integer else 'number', 'default': default,
            'min': minimum, 'max': maximum, 'step': step, 'group': group, 'help': help_text, 'effect': effect,
            'impact': group, 'unit': unit, 'display_scale': display_scale}


FIELDS = [
    number('depth_level', '模型精度档位', 5, 0, 9, 1, 'depth',
           '深度模型内部的空间计算预算（token 数），不是浮雕高度。',
           '调高：通常更慢，细节可能略多；不会让浮雕更凸。', integer=True),
    number('input_side', '深度输入最长边', 512, 128, 1536, 64, 'depth',
           '照片送入深度模型前缩放到的最长边。小原图不会被强行放大。',
           '调大：小物体和边缘的深度细节更多，推理更慢。', integer=True, unit='px'),
    {'key': 'mirror', 'label': '加入水平镜像推理', 'kind': 'boolean', 'default': True, 'group': 'fusion', 'impact': 'inference',
     'help': '额外用水平镜像的照片推理，翻回原方向后与原图结果融合。不改变模型朝向。',
     'effect': '开启：推理次数翻倍，左右偶发噪点减少，细节可能被平滑。'},
    {'key': 'scales', 'label': '输入尺度列表', 'kind': 'scales', 'default': [1.0], 'min': 0.25, 'max': 1.0, 'max_items': 4, 'required_value': 1.0, 'group': 'fusion', 'impact': 'inference',
     'help': '以输入最长边的多个比例分别推理再融合。逗号分隔，必须含1，最多4项，范围0.25–1，例如1, 0.75。重复像素输入会自动去重。',
     'effect': '加更多尺度：推理次数增加，大面积起伏更稳，细节可能被平滑。'},
    {'key': 'fusion', 'label': '深度融合方式', 'kind': 'choice', 'default': 'median', 'group': 'fusion', 'impact': 'fusion', 'options': [{'value': 'median', 'label': '中位数：抑制异常预测'}, {'value': 'mean', 'label': '平均值：更平滑，可能减弱细节'}],
     'help': '多次推理先对齐整图深度范围，再按此方式合成一份深度。切换方式可复用原始推理。',
     'effect': '中位数抑制个别异常；平均更平滑。只有一次推理时无影响。'},
    {'key': 'mask_mode', 'label': '保留区域来源', 'kind': 'choice', 'default': 'auto', 'group': 'mask', 'impact': 'mask', 'options': [{'value': 'auto', 'label': '自动识别前景'}, {'value': 'provided', 'label': '使用上传的黑白蒙版'}],
     'help': '决定“保留起伏的区域”从哪里来。上传蒙版须与图片同方向、同宽高比；按最近邻缩放。',
     'effect': '自动识别依赖分割模型；上传蒙版可精确控制：白色保留，黑色压平。'},
    number('background_flatten', '背景压平程度', 1.0, 0.0, 1.0, 0.05, 'mask',
           '保留区域之外的背景被压平的比例。主体内部不变。',
           '调高：背景越平、主体越突出；0% 与主分支相同，保留整图起伏并跳过分割。', unit='%', display_scale=100),
    number('min_area_fraction', '最小孤立区域面积', 0.02, 0.0, 1.0, 0.001, 'mask',
           '按整幅图片面积过滤自动前景中的孤立小碎块；相连的家具先合并，手工框选区域始终保留。',
           '调高：零碎小物体被压平；调低：保留更多小块，也可能保留误识别。', unit='%', display_scale=100),
    {'key': 'mask_refine', 'label': '图割修整外轮廓', 'kind': 'boolean', 'default': True, 'group': 'mask', 'impact': 'mask',
     'help': '用图割只修整联合前景的外边缘，不对靠垫、扶手等内部色块分别切割。',
     'effect': '开启：轮廓更贴合物体边缘，稍慢；关闭：直接用分割结果，边缘可能锯齿。'},
    number('amplitude', '浮雕起伏幅度', 0.32, 0.02, 1.0, 0.01, 'shape',
           '浮雕总起伏高度。画面高度记为2，默认0.32相当于画面高度的16%。',
           '调大：整体更凸、坡面更陡；只放大高度，不增加细节。'),
    number('edge_transition_fraction', '轮廓过渡宽度', 0.008, 0.0, 0.05, 0.001, 'shape',
           '前景轮廓向内的坡面宽度，按画面尺寸计算；主体内部保持不变。',
           '调大：边缘坡面更宽更柔和；调小：边缘更陡，更像切出来。', unit='%', display_scale=100),
    number('grid_side', '输出网格最长边', 512, 64, 1024, 64, 'shape',
           '输出三角网格最长边的顶点数。',
           '调大：表面更细腻、文件更大；超过深度分辨率后不再增加真实细节。', integer=True, unit='顶点'),
    number('low_percentile', '深度范围下分位', 2.0, 0.0, 20.0, 0.5, 'advanced',
           '归一化时裁去逆深度较低端（较远处）的极值；这是整图范围，不是分主体处理。',
           '调高：远处层次被压到底，近处起伏范围变大。', unit='%'),
    number('high_percentile', '深度范围上分位', 98.0, 80.0, 100.0, 0.5, 'advanced',
           '归一化时裁去逆深度较高端（较近处）的极值。',
           '调低：更多近处像素顶到最高，近处细节被削平。', unit='%'),
    number('mask_side', '蒙版处理最长边', 1024, 256, 1536, 64, 'advanced',
           '生成蒙版及图割边缘时的采样分辨率；分割网络权重与自身输入配置固定。',
           '调大：轮廓更细，处理更慢。', integer=True, unit='px'),
    number('mask_refine_radius', '轮廓修边范围', 0, 0, 12, 1, 'advanced',
           '图割允许修改的轮廓内侧带宽，0为按蒙版大小自动选择。',
           '调大：允许更大幅度修整轮廓，也可能吃掉细部。', integer=True, unit='px'),
    number('mask_refine_iterations', '图割迭代次数', 3, 1, 10, 1, 'advanced',
           '图割迭代次数；不能修复模型完全漏掉的主体。',
           '调高：轮廓可能更准，也更慢。', integer=True),
    number('threads', 'CPU推理线程', 4, 1, 8, 1, 'advanced',
           'CPU 推理线程数。实验台一次只运行一个任务。',
           '只影响速度，不影响模型形状；更多线程未必更快。', integer=True),
    {'key': 'use_cache', 'label': '复用计算缓存', 'kind': 'boolean', 'default': True, 'group': 'advanced', 'impact': 'performance',
     'help': '复用之前算过的分割和深度。关闭后本次强制重新推理，完成结果仍会保存。',
     'effect': '关闭：每次重新推理，耗时真实但更慢；模型形状不变。'},
]

DEFAULTS = {field['key']: copy.deepcopy(field['default']) for field in FIELDS}

# Must stay equivalent to backend/anyimage_worker.py: whole image at 256 px, level 0, one pass,
# 2–98 percentiles, 0.32 amplitude, no background flattening. Threads stay at the lab default (production
# uses 1); they only change speed and the depth cache key, not the shape. Kept in sync by hand.
MAIN_BRANCH_PRESET = {'depth_level': 0, 'input_side': 256, 'grid_side': 256, 'mirror': False, 'scales': [1.0],
                      'background_flatten': 0.0}


def validate_parameters(overrides=None):
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError('参数必须是对象。')
    unknown = set(overrides) - set(DEFAULTS)
    if unknown:
        raise ValueError('不支持的参数：' + ', '.join(sorted(map(str, unknown))))
    values = copy.deepcopy(DEFAULTS)
    values.update(overrides)
    for field in FIELDS:
        key, kind, value = field['key'], field['kind'], values[field['key']]
        if kind in ('number', 'integer'):
            allowed = type(value) is int if kind == 'integer' else type(value) in (int, float)
            if not allowed or not math.isfinite(value) or not field['min'] <= value <= field['max']:
                raise ValueError(f"{field['label']}必须在 {field['min']}–{field['max']} 范围内。")
            values[key] = int(value) if kind == 'integer' else float(value)
        elif kind == 'boolean':
            if type(value) is not bool:
                raise ValueError(f"{field['label']}必须为开启或关闭。")
        elif kind == 'choice':
            if value not in [option['value'] for option in field['options']]:
                raise ValueError(f"{field['label']}的选项无效。")
        elif kind == 'scales':
            if (not isinstance(value, list) or not 1 <= len(value) <= 4
                    or any(type(item) not in (int, float) or not math.isfinite(item) or not 0.25 <= item <= 1 for item in value)
                    or 1.0 not in value or len(set(value)) != len(value)):
                raise ValueError('尺度需要1–4个不重复数值，范围0.25–1，且必须包含1。')
            values[key] = sorted((float(item) for item in value), reverse=True)
    if values['low_percentile'] >= values['high_percentile']:
        raise ValueError('深度下分位必须小于上分位。')
    return values


def validate_boxes(boxes=None):
    if boxes is None:
        return []
    if not isinstance(boxes, list) or len(boxes) > 8:
        raise ValueError('最多添加8个保留区域框。')
    result = []
    for box in boxes:
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in box)):
            raise ValueError('框选区域需要4个有限的归一化坐标。')
        x0, y0, x1, y1 = map(float, box)
        if not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
            raise ValueError('框选区域必须位于图片内，且宽高大于0。')
        result.append([x0, y0, x1, y1])
    return result


def config_payload():
    preset_overrides = [
        ('main', '主分支默认', MAIN_BRANCH_PRESET),
        ('average', '平均', {'depth_level': 0, 'input_side': 256, 'grid_side': 256, 'mirror': False, 'scales': [1.0]}),
        ('fine', '精细', {}),
        ('finer', '更精细', {'depth_level': 7, 'input_side': 768, 'grid_side': 768, 'scales': [1.0, 0.75]}),
        ('maximal', '最高', {'depth_level': 9, 'input_side': 1536, 'grid_side': 1024, 'scales': [1.0, 0.875, 0.75, 0.625]}),
    ]
    return {
        'groups': copy.deepcopy(GROUPS), 'fields': copy.deepcopy(FIELDS), 'defaults': copy.deepcopy(DEFAULTS),
        'presets': [{'id': key, 'label': label, 'parameters': validate_parameters(params)} for key, label, params in preset_overrides],
    }
