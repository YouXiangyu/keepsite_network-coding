# 主体浮雕：独立本地原型

输入照片 → 自动语义分割与保守图割 → 每个主体单独裁图/估深 → 主体内部归一化 → 原图坐标合成 → 带纹理 GLB。

本组件没有接入业务 API、生成队列或数据库，不修改默认收藏，不上传图片，不启动 AWS。代码与测试放在此目录，图片、权重和验证产物放在被 Git 忽略的 `.cache/`。

## 为什么要这样处理

当前生产 `backend/anyimage_worker.py` 把整张图缩到最长边 256，送入官方 AnyImage MoGe2，随后把整图逆深度的 2–98 百分位映射到 `[-0.16, +0.16]`。近处地板比远处物体凸出可能符合相机距离，却不符合“主体突出、背景铺平”的浮雕目标；它不是直接按白色或黑色决定前后。

本原型先用 SegFormer B0 的 ADE20K 语义类别选出物体，按类别连通域分区，再用 OpenCV GraphCut 保守修边。每个主体的蒙版外填中性灰，保留少量边距，独立送入已有 MoGe2 ONNX 模型。只在该主体内计算逆深度百分位，映射到正高度；背景恒为 0。各主体顶面不共享跨主体三角形，轮廓侧壁落到背景平面，地板不会被床的顶点拉高。

主体内部使用掩膜归一化平滑，并在轮廓数像素内向最低离面高度收边，以减轻高侧壁沿像素边缘产生的突兀断层；不会引入相邻物体或背景高度，也不改变原始深度文件。栅格轮廓本身没有被矢量化，极细区域的起伏可能减弱。

这里的坐标是艺术浮雕坐标：画面高度为 2，默认主体离面 `lift=0.04`、内部起伏 `relief=0.18`。它们不是米、厘米或估计的物理距离。

## 文件边界

| 文件 | 职责 |
| --- | --- |
| `segmentation.py` | 本地语义分割、类别/连通域筛选、图割修边、显式框选或蒙版 |
| `geometry.py` | 蒙版内高度计算、独立网格、背景与侧壁、GLB 导出 |
| `run.py` | 离线运行入口，复用现有 CPU 会话和官方推理接口 |
| `test_geometry.py`、`test_segmentation.py` | 几何方向、隔离、孔洞、白色主体、背景、输入边界等测试 |
| `viewer.html`、`viewer.js` | 独立本地双模型对照，纹理开关与同步视角 |

没有改动生产 worker 或 `.cache/anyimage` 中的上游源码。单独提供透明 PNG 给旧 worker 并不等同于此组件：旧流程不会用 alpha 限制深度归一化或网格。

## 本机准备

从仓库根目录运行。已验证环境是 Windows、Python 3.13.11、CPU 推理，具体依赖在本目录 `requirements.txt`。本次复用 `backend/.venv` 以及已有 `.cache/anyimage-deps`，没有安装或升级现有项目依赖。

需要已有的 AnyImage 固定源码与 MoGe2 权重。项目的 `scripts/setup-anyimage.ps1` 是原有准备入口；已有环境不要重复安装。

自动分割另需以下本地文件，入口禁止隐式联网下载：

```text
.cache/models/segformer-b0-ade/config.json
.cache/models/segformer-b0-ade/preprocessor_config.json
.cache/models/segformer-b0-ade/model.safetensors
```

来源为 [NVIDIA SegFormer B0](https://huggingface.co/nvidia/segformer-b0-finetuned-ade-512-512)，固定 revision `489d5cd81a0b59fab9b7ea758d3548ebe99677da`。本次权重 SHA-256 与 Hugging Face 官方 LFS 元数据一致：`6ae39addd01de6b1b8bde2cf677d43a5cd733424b8d186de3f95d1c51fee23f9`。

若本机尚未下载，可显式执行下面的 PowerShell 命令。目录已存在时先检查，不覆盖已有文件：

```powershell
$modelRoot = '.cache/models/segformer-b0-ade'
New-Item -ItemType Directory -Path $modelRoot -ErrorAction Stop | Out-Null
$revision = '489d5cd81a0b59fab9b7ea758d3548ebe99677da'
foreach ($file in @('config.json', 'preprocessor_config.json', 'model.safetensors')) {
    Invoke-WebRequest "https://huggingface.co/nvidia/segformer-b0-finetuned-ade-512-512/resolve/$revision/$file" -OutFile (Join-Path $modelRoot $file)
}
Get-FileHash -Algorithm SHA256 (Join-Path $modelRoot 'model.safetensors')
```

该权重的[上游许可](https://github.com/NVlabs/SegFormer/blob/master/LICENSE)限定研究/评估用途，因此本原型只做本地评估。未来接入商业产品前，应替换为适用的分割模型或取得相应授权；权重不入 Git。

## 运行

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:HF_HOME = Join-Path (Get-Location) '.cache/subject-relief/hf'
$env:TORCH_HOME = Join-Path (Get-Location) '.cache/subject-relief/torch'
$env:OMP_NUM_THREADS = '4'
$env:MKL_NUM_THREADS = '4'
$env:MEMOVERSE_ORT_THREADS = '2'
& backend/.venv/Scripts/python.exe -B -m experiments.subject_relief.run INPUT.jpg .cache/subject-relief/result-01
```

输出目录必须不存在。失败时保留中间结果供诊断，重新运行请选新目录。未指定 `--baseline-model` 时，用原有生产 worker 另生成一份整图基线。

可选参数：

- `--labels bed,sofa,table`：只提升模型支持的指定类别；墙、地板等结构面不能作为自动主体。
- `--box 'device=1400,1100,1945,2050'`：框选后运行 GrabCut。坐标为原图 EXIF 转正后的半开像素矩形 `x0,y0,x1,y1`，框内需包含少量背景。
- `--mask 'device=MASK.png'`：用明确蒙版校正；尺寸须与 EXIF 转正后的原图相同，亮度与 alpha 均至少 128 的像素属于主体。
- `--manual-only`：仅使用显式框选/蒙版，不加载语义模型。
- `--no-refine`：显式跳过自动图割修边，适用于主体覆盖整幅画面、没有足够背景样本等情况；不会静默忽略图割失败。
- `--baseline-model EXISTING.glb`：比较已有整图模型，不重新生成基线。已有 GLB 的来源模型 hash 不作推测。
- `--lift 0.04 --relief 0.18`：主体最低离面高度与内部起伏。无有效深度时明确失败；有效深度恒定时保留为抬高的平面并标记。
- `--grid-side 384 --max-side 1024 --max-subjects 12`：网格长边、工作图长边和主体上限。

人工蒙版优先占用像素，自动主体让出对应区域；人工蒙版相互重叠则报错。每个主体共用配置的浮雕幅度，不保留各次独立深度预测之间不可比的绝对尺度。

## 查看结果

`report.json` 记录输入/模型 hash、逐主体来源、实际推理次数、有效深度比例、耗时、网格与限制。`subject-XX-depth.npy` 是模型输出深度，`height.npy` 是合成浮雕高度，两者不能混用。

另有 `original.png`、`overlay.png`、`owners.png`、`semantic-labels.png`、逐主体 mask/input，以及 `model.glb` 与 `baseline/model.glb`。实际图片只保留在本机缓存中。

本地查看页使用项目已安装的 Three.js 和 Rolldown，不引入 CDN：

```powershell
$result = '.cache/subject-relief/result-01'
& ./demo/node_modules/.bin/rolldown.cmd experiments/subject_relief/viewer.js --format esm --platform browser --file "$result/viewer.bundle.js"
Copy-Item experiments/subject_relief/viewer.html "$result/index.html"
& backend/.venv/Scripts/python.exe -B -m http.server 4182 --bind 127.0.0.1 --directory .cache/subject-relief
```

打开 `http://127.0.0.1:4182/result-01/index.html`。查看后结束临时 HTTP 服务。关掉“照片纹理”可以单独检查几何，正面/斜侧按钮同步控制两侧，倍率只影响显示、不改模型文件。

## 2026-10-19 酒店房间验证

使用默认收藏“房间”的本地原图，保持原图和业务数据不变。旧模型使用该收藏原有 GLB，长宽比核对一致。

| 结果 | 主体推理 | 总耗时 | 背景最大绝对高度 | 主体最低高度 |
| --- | ---: | ---: | ---: | ---: |
| `.cache/subject-relief/room-auto-v1`，纯自动 | 7 次 | 46.153 秒 | 0 | 约 0.04 |
| `.cache/subject-relief/room-corrected-v1`，补充一次设备框选 | 8 次 | 31.338 秒 | 0 | 约 0.04 |
| `.cache/subject-relief/room-corrected-v2`，框选并加入边缘平滑/收边（当前） | 8 次 | 41.193 秒 | 0 | 约 0.04 |

CPU/线程与热身状态不同于服务器，且首次导入/加载有额外开销，上述时间不作算法性能比较。所有主体确实分别调用 MoGe2，网格顶面覆盖率均为 100%。本轮 48 项组件测试通过（33 项分割、15 项几何与合成）。当前 GLB 为 9,164,492 bytes，包含平面背景与 8 个主体区域。

自动结果中的床与地板已分开，但白色立式设备被错误标成 table 并与床头柜连在一起；沙发也被拆成多个语义区。这些是观察到的分割误差，不能把“7 个区域”说成“正确识别了 7 个独立物体”。校正版使用 `--box 'air-device=1400,1100,1945,2050'`，使白色设备单独生成；该框由人工指定，不能计为自动识别成功。

另外人工选取了地板、墙、床、白色设备和沙发坐垫内部的固定矩形作为核验点。两版地板/墙核验区均保持零高度，床/坐垫核验区均凸起；自动版设备内部约 97.96% 像素凸起，仍有漏选孔洞，校正版达到 100%。这只是所选内部区域的检查，不是整幅图分割精度或完整物体 IoU。各结果目录的 `verification.json` 记录矩形坐标与 GLB 几何读回检查。

## 当前边界

- 自动分割是语义类别加连通域，同类接触的物体可能合并，同一物体也可能被拆成不同类别。ADE20K 没有专门的空调/杯子类别；玻璃器皿、食物等标签不保证覆盖所有具体物件。
- 图割只能改善有限轮廓，不能理解“所有应该凸起的物体”。自动漏检需要框选或蒙版；不会偷偷用亮度替代语义或补出伪深度。
- 背景被艺术化压平，主体轮廓有明确侧壁，错误或粗糙的分割会表现为接缝、断层。尚未做大样本视觉评估、实例合并或精细边缘优化。
- 裁切会改变模型看到的上下文；它不保证比整图估深更准确。独立归一化用于突出物体，不代表真实遮挡顺序或世界坐标。
- 单张图不能恢复被遮挡的背面；输出是供查看的 2.5D 浮雕表面，不是完整 360° 模型或可直接打印的封闭实体。

测试命令：

```powershell
& backend/.venv/Scripts/python.exe -B -m pytest experiments/subject_relief -q --basetemp=.cache/subject-relief/pytest-new -p no:cacheprovider
```

本轮收尾时，以下新建测试临时目录的删除被自动审批策略拦截（工具仅返回 blocked by policy），已停止重试，没有绕过：`.cache/subject-relief/` 下的 `pytest-all-01`、`pytest-geometry-02`、`pytest-segmentation-20261019-a`、`pytest-segmentation-20261019-b`、`pytest-bevel-01`。它们与保留供对照的图片、模型和报告目录分别管理，均不纳入 Git。
