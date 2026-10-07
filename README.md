<p><img src="frameseek/static/app-icon.png" width="96" alt="FrameSeek 图标"></p>

# FrameSeek

放入一张截图，找到相似的视频画面、所在文件和对应时间，再点击结果，通过 Emby 从命中位置开始播放。

项目使用 DINOv3 ViT-L/16 提取图片特征，Qdrant 负责检索，SQLite 保存文件信息和搜索历史。可以在本地用显卡建立索引，再交给 NAS 提供搜索服务。


## 可以做什么

- 上传、拖拽或粘贴截图，按目录缩小搜索范围。
- 读取 Emby 生成的 BIF 缩略图，利用已有的视频画面建立索引。
- 同时检索 JPG、PNG、WebP 等普通图片，可选择全部、仅 BIF 视频画面或仅图片。
- 将同一视频的命中放在一起，按相似度或时间排序，查看附近画面。
- 保存搜索截图和结果，随时查看历史记录。
- 自动处理新增或修改的 BIF 和图片，中断后继续处理。
- 联动 Emby，从截图对应的时间点播放视频，支持独立播放窗口和 Emby 网页端。

普通图片支持 JPG/JPEG、PNG、WebP、BMP、GIF 和 TIFF。与 BIF 共用监控目录，下一次扫描会自动加入处理；搜索时可按类型和目录组合筛选。动图和多页 TIFF 只索引第一张画面，图片结果可查看预览。已有 BIF 索引无需重新编码。

## 搜索效果

<table>
  <tr>
    <td width="50%"><img src="demo/Image_2026-10-07_17-02-56_1mbdydk3.ahs.png" alt="搜索效果" width="100%"></td>
    <td width="50%"><img src="demo/Image_2026-10-07_17-07-35_4ahnwbh5.dds.png" alt="搜索效果" width="100%"></td>
  </tr>
</table>

## 与 Emby 联动

支持直接读取 Emby 生成的 BIF 视频缩略图，从其中的画面建立搜索索引，无需重新解码原视频，也无需将缩略图展开成大量 JPEG 文件。在“设置”中填写存放 BIF 的目录，系统会查找该目录及子目录中的 `.bif` 文件。

在“设置 → Emby 播放”中填写 Emby 服务地址和 API Key，点击“读取用户与网页设备”，选择播放用户和方式，再开启“点击搜索结果播放”。API 和网页共用一个地址，应用和浏览器都需能访问。API Key 在 Emby 管理后台创建。

搜索到画面后，点击结果即可找到对应视频，并从命中时间播放：

- **独立播放窗口**：新窗口直接播放视频。
- **Emby 网页端**：打开 Emby 原生播放器，通过 API 播放并跳转到命中位置。可以每次新开网页端，也可以优先使用最近活跃的网页端，没有则新开；还可指定浏览器设备。

使用网页端播放时，先在 Emby 网页端登录所选用户。同一浏览器的多个标签可能共享会话。Emby 媒体库需包含对应视频；找不到视频时，可在设置中刷新视频映射。


## 准备模型

先手动下载 [DINOv3 ViT-L/16 LVD-1689M](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m)，放到 `models/dinov3-vitl16`。目录中应有 `config.json` 和完整的 `.safetensors` 权重；分片模型还需要 `.safetensors.index.json`。模型页面需要申请访问。


## 在 NAS 上运行

使用 [`compose.ghcr.yml`](compose.ghcr.yml) 拉取 GitHub 构建好的 CPU 镜像，适用于 Intel/AMD NAS。

把 Compose 文件放到项目目录，修改其中的媒体目录。镜像地址已设为 `ghcr.io/ting1e/frameseek:latest`。路径、端口、内存等启动配置直接写在 Compose；监控目录、处理线程和搜索选项在网站中设置。

Compose 只填写媒体挂载：

```yaml
volumes:
  - /mnt/videos:/mnt/videos:ro
  - /mnt/archive:/mnt/archive:ro
```

下载好模型、改好媒体目录后，在项目目录直接启动：

```bash
docker compose -f compose.ghcr.yml up -d
```

首次启动会自动创建数据目录、生成模型清单和登录信息，再启动网站及 Qdrant。登录信息查看：

```bash
docker compose -f compose.ghcr.yml exec app cat /data/credentials.txt
```

默认用户名为 `admin`，可在 Compose 中修改；重启沿用原来的登录信息。数据和首次启动配置保存在 `data`，模型目录保持只读。

密码由 Python `secrets` 使用系统安全随机源生成，包含 144 位随机性。登录验证使用随机盐和 60 万次 PBKDF2-SHA256；首次生成的明文密码保存在 `data/credentials.txt`，Linux 权限为 `600`。保存好密码后可以删除这个文件，登录仍可正常使用。

应用端口为 `127.0.0.1:18443`，接入 HTTPS 反向代理后访问。镜像若为私有包，先登录 `ghcr.io`；公开包可以直接拉取。Qdrant 数据目录放在 SSD/NVMe 上。

登录网站后，在“设置”中填写需要监控的完整目录，每行一个，例如 `/mnt/videos` 或 `/mnt/videos/国产剧`，修改后自动保存到 SQLite，下一轮扫描生效。取消监控会保留已有索引；添加尚未挂载的宿主机目录时，先修改 Compose 挂载。点击后台任务中的手动扫描，或开启自动更新。NAS CPU 可以从每批 1 帧、4 个推理线程、1 个解码线程开始；修改线程和批次后重启应用。

### 内存模式

| 模式 | 读取方式 | Qdrant 上限 |
|---|---|---|
| `low_memory` | SSD 按需读取 | 2 GiB |
| `high_memory` | 缓存预热 | 12 GiB |

NAS 部署默认大内存模式，应用另有 4 GiB 上限。切换时在 Compose 中同时修改 Qdrant 的 `mem_limit` 和应用的 `IMGS_MODE`，重建容器即可继续使用原索引。

### 更新镜像

在网页暂停后台任务，然后执行：

```bash
docker compose -f compose.ghcr.yml pull
docker compose -f compose.ghcr.yml up -d
```

更新后恢复后台任务。需要固定版本时，将镜像的 `latest` 改为版本号或 `sha-<完整提交SHA>`。模型、索引和网站配置都保存在挂载目录中。

镜像将 PyTorch、其余依赖和程序分层保存。只修改页面或程序时，构建会复用依赖层；更新时 Docker 只下载本机缺少的新层，模型和索引不随镜像下载。GitHub 构建完成并通过启动检查后，直接发布同一个镜像。仅修改 README 或 demo 图片不会触发构建。

## 本地开发和建索引

使用 Python 3.10+，显卡推理需要安装对应的 PyTorch CUDA 版本。

```bash
python -m venv .venv
# 激活虚拟环境后执行
pip install -e '.[ml,test]'
frameseek init
frameseek prepare-model
frameseek serve
```

在 `.env` 中设置 `IMGS_DEVICE`，媒体目录和处理批次在网站配置。旧目录映射会保存到 SQLite，现有索引继续使用，无需重新编码。命令行也可以建索引：

```bash
frameseek scan
frameseek index
frameseek index --verify-only
frameseek status
```

从 NAS 复制 BIF 时，将 [`sync.example.json`](sync.example.json) 复制为 `sync.local.json`，填写 SSH 连接和远程目录，使用系统 SSH 密钥连接：

```bash
frameseek inventory
frameseek sync
```

`frameseek backup` 可以备份 SQLite 和 Qdrant 快照，搜索历史和网站配置也会一起保存。

所有 Compose 都直接使用 GHCR 镜像：`compose.yml` 默认 2 GiB，`compose.ghcr.yml` 默认 12 GiB。本地 HTTP 测试使用 `compose.yml` 加 `compose.local.yml`（Docker Compose 2.24.4+），首次启动自动生成登录信息。内存模式直接修改 Compose 中的 `mem_limit` 和 `IMGS_MODE`。

源码在 `frameseek/`，前端文件在 `frameseek/static/`，测试在 `tests/`，开发脚本在 `scripts/`。本地验证新镜像可运行 `docker build -t frameseek:ci .`；部署无需本地构建。

`theme.css` 是 daisyUI 样式构建入口。修改主题或前端使用的样式类后，用 Node.js 20+ 运行 `npm ci` 和 `npm run build:css`。静态文件已包含在镜像中，NAS 直接运行即可。
