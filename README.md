# 片场 · RunPod Drama Studio v0.1

基于 Python + FastAPI + SQLite + 原生 HTML/CSS/JS 的个人短片制作工作台。无需前端构建，无需本地 GPU。面向个人/家庭可信局域网，单进程运行。

## 已实现

- 多项目、分镜编辑、数字排序、5/10 秒镜头。
- 分别编辑图片和动作提示词；调用 Qwen 优化提示词，编辑确认后保存。
- FLUX Schnell 关键帧生成；本地 PNG/JPEG/WebP 上传并转为 PNG。
- WAN 2.5 图生视频，720p；生成任务异步排队、进度查看、单镜重做。
- SQLite 持久化请求快照、任务 ID、返回费用和素材版本。历史版本可下载。
- 已知 ID 的云端任务在重启后继续轮询。网络故障后可点击恢复查询。
- 提交结果不确定时暂停自动提交，允许填写控制台的原任务 ID 恢复；不会盲目重复付费请求。
- FFmpeg 统一画幅、帧率、音频格式后合成 MP4，保留原片音频；无声镜头补静音。
- 按镜头旁白文本生成独立 SRT 字幕；分镜清单可导出 JSON。
- 免费演示模式：占位图 + 静态视频，验证全流程。演示模式不调用任何云端模型。

## 1. Docker 启动（推荐）

在已经安装 Docker Engine 和 Compose 插件的 Linux VM/LXC 中：

```bash
cp .env.example .env
docker compose up -d --build
```

浏览器访问 http://127.0.0.1:8000 。第一次构建需要下载镜像与依赖。
默认 `DEMO_MODE=true`。点击「载入示例项目」可建立两个分镜。
按镜头点击「生成图」→「生成视频」，全部完成后点击「合成初剪」。
记录区下载 MP4 和 SRT。演示视频是静态占位视频，不是 AI 动画。

如果从其他电脑/iPad 访问 PVE 内的服务，在 `.env` 中把 `BIND_ADDRESS`
设为这台 VM/LXC 的实际局域网 IP，然后执行 `docker compose up -d`。
浏览器访问 `http://该服务器IP:8000`。

初始建议资源：2 vCPU / 2–4 GB RAM / 10 GB 可用空间。FFmpeg 转码使用 CPU；
长片或多次重做会增加磁盘占用，按实际项目扩容。

## 2. 切换真实 RunPod API

编辑服务端 `.env`：

```dotenv
DEMO_MODE=false
RUNPOD_API_KEY=在服务端填入你自己的Key
BIND_ADDRESS=127.0.0.1
PUBLIC_MEDIA_BASE_URL=
```

执行：

```bash
docker compose up -d --force-recreate
```

API Key 不存入数据库、不发给浏览器、不写进导出的 JSON。
真实模式中「优化提示词」「生成图」「生成视频」都会使用云端 API，可能产生费用。
建议先新建一个测试项目，做一个 5 秒镜头，确认账户、网络和模型接口正常。
不要直接使用演示项目里的占位图作为正式影片素材。

当前接口：
- Qwen: `qwen3-32b-awq/openai/v1/chat/completions`
- Image: `black-forest-labs-flux-1-schnell/run`
- Video: `wan-2-5/run`，使用 `resolution: 720p`
- 查询: `<endpoint>/status/<job_id>`

模型是否仍开放、账户是否可用以及输出质量，需要真实账户集成验证。
接口根据 2026-11-17 查阅的 RunPod 官方模型文档编写：
https://docs.runpod.io/public-endpoints/models/flux-schnell
https://docs.runpod.io/public-endpoints/models/wan-2-5
https://docs.runpod.io/tutorials/public-endpoints/text-to-video-pipeline

## 3. 上传关键帧如何供云端读取

本地上传可在演示模式直接用；真实视频生成要求公网可读取图片 URL。
推荐在你现有 Nginx Proxy Manager / Cloudflare Tunnel 上建立一个素材域名，
例如 `https://studio-media.example.com`，只开放 `/media/` 路由。
然后设置 `PUBLIC_MEDIA_BASE_URL=https://studio-media.example.com`。
WAN 会访问 `https://studio-media.example.com/media/<随机文件名>.png`。
此地址应无需交互登录，不能被 Cloudflare Access 登录页阻挡。

反向代理示例（域名与 upstream 按实际环境调整）：

```nginx
server {
    listen 80;
    server_name studio-media.example.com;
    location /media/ {
        proxy_pass http://studio:8000;
    }
    location / { return 404; }
}
```

随机素材 URL 不是完整的访问授权机制，持有链接的人可以读取对应素材。
如果没有配置这个地址，FLUX 生成的图可暂用模型返回的公网 URL；
该 URL 可能过期，长期使用建议设置自己的素材入口。

本版没有用户登录和多租户权限，不要把完整工作台、`/api/` 或 `/docs`
直接暴露到公网。远程访问工作台用你已有的 VPN/受保护反向代理。
素材域名只开放 `/media/`，管理入口单独保护。

## 4. 日常使用

1. 创建项目并写统一风格。
2. 添加镜头，填写图片、动作提示词、旁白字幕和时长。修改「排序」决定剪辑顺序。
3. 生成或上传关键帧，检查人物和构图后再生成视频。
4. 失败或不满意时按单镜重做。重做关键帧会清除当前视频选择，避免误用旧画面。
5. 全部镜头完成后合成。输出 1280×720、24fps、H.264/AAC MP4。
6. 下载成片与 SRT，在剪映等工具中继续配音、混音、精剪。

编辑提示词不会自动重做素材；旧素材仍可用于合成，需自行判断是否重做。
任务运行中锁定该镜头的编辑、删除和新生成，防止重复提交。
历史素材保留在制作记录中；暂不支持一键回选历史版本。
JSON 导出用作记录，暂不提供 JSON 导入。

## 5. 超时、恢复与费用

- HTTP 查询失败 → 暂停任务 → 点击「恢复查询」，使用同一个云端 ID。
- 提交超时 / 提交时服务重启 → 「提交结果待核对」。先去 RunPod 控制台核对，
  找到原任务 ID 后点击「填入任务 ID」。不可在未核对时反复提交。
- 「放弃跟踪」仅停止本地跟踪，不取消云端任务，云端仍可能计费。
- 云端失败后可以重新点击生成，这属于新的任务。
- 已返回的 API 费用显示在项目统计中，不是账户账单；不包含 Qwen 提示词调用费用，
  也不包含尚未回传费用或已放弃跟踪任务的费用。没有预算上限功能。
- 仅运行一个 Uvicorn worker / 一个容器副本；多副本会重复抢任务，本版不支持。
- 未知状态的云端任务持续查询，没有自动取消；需要人工在 RunPod 处理异常任务。

## 6. 持久化、备份与 NAS

默认数据存在 Docker named volume `studio-data`，包含 `studio.db` 与 `media/`。
重新构建镜像不会删除数据；不要执行 `docker compose down -v`。
建议 SQLite 留在本机磁盘。NAS 用作备份与成片归档，不建议把 SQLite 直接放在 NFS。

简单一致性备份：

```bash
docker compose stop
mkdir -p backup
docker compose cp studio:/data/. ./backup/
docker compose start
```

再把 `backup/` 同步至 NAS。恢复前停止服务，把数据库与素材作为同一批次恢复。
目前没有自动 NAS 同步或素材清理任务。历史生成结果会持续占用磁盘。

## 7. 不使用 Docker

要求 Python 3.11+、FFmpeg（含 ffprobe）：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export DEMO_MODE=true
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

本地 Python 启动不会自动读取 `.env`，需通过 shell 设置变量；Docker Compose 会读取 `.env`。
Windows PowerShell 中用 `$env:DEMO_MODE="true"` 设置变量，并确保 FFmpeg 在 PATH 中。

## 8. 验证与边界

```bash
python -m pytest -q tests
```

覆盖演示生成/视频/合成/SRT闭环、输入校验、跨站写入限制、提交不确定时不重发、
已知任务 ID 恢复和重启状态处理。RunPod 请求异常测试使用 mock，不消耗额度。

本版不包含：自动剧本拆镜、角色身份一致性控制、TTS、音乐混音、口型同步、
字幕烧录、复杂转场、自动批量生成全片、多用户登录、云端任务取消。
本版定位是可用的个人短片工作台；先验证一个短片的制作体验，再扩展这些能力。
