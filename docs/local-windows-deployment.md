# Windows 本机部署

本分支基于 El1mir 的 Ember UI 版本 `c011bbdf`。以下入口将正式构建界面和 API 放在同一个本机地址，不需要一直运行 Vite 开发服务器。

## 本分支改动

- 删除更新模型供应商时输出完整配置的日志，避免新 API Key 进入日志。
- 后端和开发服务器默认只监听 `127.0.0.1`。
- 正式运行入口检查 Host、浏览器 Origin 和跨站 API 请求，适用于单机浏览器使用。
- 提供后台启动、健康检查、带本机控制 token 的正常退出入口。
- 默认开启画面理解，使用智能采样，默认全片最多 60 帧，仍可在生成设置中调整。
- 单集和批量生成默认输出原片截图与时间戳跳转链接，可在生成设置中关闭。
- 云端转写支持 DashScope 原生文件转写，可使用 `qwen3-asr-flash-filetrans`，自动上传至阿里云临时存储、查询异步任务，并保留服务返回的句级时间轴。
- 已完成的笔记可直接下载 PDF，保留中文、真实截图、表格和时间戳链接；复用网页已有的 KaTeX 排版行内公式、独立公式、矩阵等，再通过本机浏览器打印。全程本地渲染，不调用模型或外网。错误或不受支持的公式显示提示并保留原文。
- 修复设置子页面直接打开和刷新时跳回通用设置的问题。

这不是多人或公网服务的鉴权方案。浏览器扩展、远程设备和自定义来源需要另行设计访问策略；不要为方便而直接放开任意来源。

## 环境

Windows，Python 3.11、uv、Node.js 22 或更新版本、pnpm，以及 PATH 中可运行的 FFmpeg。PDF 导出需要已安装的 Microsoft Edge（Windows 通常自带）或 Google Chrome。

所有以下命令均在仓库根目录执行。目录可以自行选择，启动脚本按自身位置查找文件。

### 1. 安装后端

```powershell
uv venv --python 3.11 backend/.venv
uv pip install --python backend/.venv/Scripts/python.exe -r backend/requirements.txt "setuptools<81"
```

`setuptools<81` 用于兼容当前 CTranslate2 依赖的 `pkg_resources`。

### 2. 配置并构建界面

创建根目录 `.env`，内容为：

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:3015/api
VITE_SCREENSHOT_BASE_URL=http://127.0.0.1:3015/static/screenshots
VITE_FRONTEND_PORT=3015
```

创建 `backend/.env`，内容为：

```dotenv
ENV=production
BACKEND_HOST=127.0.0.1
BACKEND_PORT=3015
STATIC=/static
OUT_DIR=./static/screenshots
NOTE_OUTPUT_DIR=note_results
IMAGE_BASE_URL=/static/screenshots
DATA_DIR=data
TRANSCRIBER_TYPE=fast-whisper
WHISPER_MODEL_SIZE=small
```

`.env` 中不填写模型密钥。模型服务商、Key、模型名称和全局模型在应用设置页录入。

```powershell
cd BillNote_frontend
pnpm install --frozen-lockfile --ignore-scripts
pnpm exec vite build
cd ..
```

PDF 导出使用 `playwright-core` 控制独立的无界面浏览器，不会使用个人浏览器资料，也不自动下载浏览器。依次查找 Edge、Chrome 和已安装的 Playwright Chromium；特殊安装路径可通过启动 BiliNote 前设置 `BILINOTE_PDF_BROWSER` 指向浏览器可执行文件。Linux 容器等没有浏览器的环境可在前端目录运行 `pnpm exec playwright-core install --with-deps chromium`。升级时重新安装 `backend/requirements.txt` 和锁定的前端依赖，不能只替换前端 `dist`。

公式支持 `$...$`、`$$...$$`、`\(...\)`、`\[...\]`，代码块和行内代码中的分隔符保持原样。KaTeX 支持常用数学 LaTeX，不执行任意 TeX 包；宏展开与公式长度有限制。字体和截图嵌入文档，页面脚本、外部资源请求及 KaTeX 受信任命令均关闭。

### 3. 启动和停止

双击根目录的 **启动 BiliNote.cmd**，或运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./Start-BiliNote.ps1
```

打开 <http://127.0.0.1:3015>。停止时双击 **停止 BiliNote.cmd**。

脚本会从 `backend` 工作目录直接运行 `local_server.py`。不要换成 `uvicorn local_server:app`，否则内置停止入口无法控制外部创建的 Uvicorn 实例。`run.bat` 是保留的上游开发入口，与上述正式界面入口不同。

首次使用请在「AI 模型设置」添加支持图片输入的模型并激活为全局模型；无字幕视频还需要在「音频转写」准备本地模型或配置兼容 ASR。B站要求登录时，在「下载配置」保存自己的 Cookie。

## 本机文件

- 模型配置和任务数据：`backend/bili_note.db`。
- Cookie、转写等设置：`backend/config/`。
- 笔记和媒体：`backend/note_results/`、`backend/static/`、`backend/uploads/`。
- 启动状态、日志和缓存：`.runtime/` 与 `backend/logs/`。
- 正常退出使用的本机控制 token：`backend/.local-stop-token`，启动时生成，不随仓库分发。

这些内容不应提交。备份前先停止程序，然后复制数据库、配置、笔记和媒体目录。数据库和配置含凭据，分享日志和备份前应自行检查。Windows 使用者可进一步用目录权限限制其他本机账户的访问。

## 已做的验证

2026-11-05 在 Windows 本机完成：

- 前端 `tsc -p tsconfig.app.json --noEmit` 和正式构建。
- 本机请求边界、智能抽帧、媒体保护和失败重试共 137 项相关检查。
- 实际 HTTP 请求的 Host、Origin、跨站 GET、凭据打码、停止鉴权与页面回退检查。
- 不含个人信息的短测试视频：本地 Whisper small 转写、智能抽帧、真实云端视觉请求；模型正确区分了红色正方形与蓝色圆形。
- 设置保存、模型选择持久化、设置页刷新，以及正常停止和重启。

以上是本地验证记录，未配置公开 CI，也不代表已对任意视频或所有运行环境做全面验证。测试没有替用户登录 B站或发表评论。

相关后端检查可复跑：

```powershell
cd backend
uv pip install --python .venv/Scripts/python.exe pytest==8.4.2
./.venv/Scripts/python.exe -m pytest tests/999-local-deployment tests/002-video-understanding-sampling tests/007-note-archive-and-media-cache tests/011-failed-task-retry -q --import-mode=importlib
```
