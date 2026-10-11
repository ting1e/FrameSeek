# Embedding API

在“设置 → Embedding API”生成 Token，再开启服务。Token 使用 `Authorization: Bearer ...` 请求头传递。替换 Token 或关闭服务后，旧凭据立即失效。

上传 BIF 或图片：

```bash
curl -X POST 'https://your-server/api/v1/embeddings/upload' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@example.bif'
```

读取媒体目录内的文件：

```bash
curl -X POST 'https://your-server/api/v1/embeddings/file' \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"path":"/mnt/videos/example.bif"}'
```

路径须在网站配置的媒体目录内，排除目录不可读取。图片最大 64 MiB，上传最大 128 MiB；BIF 最多 100000 帧。GIF、TIFF 使用第一帧。

图片和不超过 8 帧的小 BIF 在资源空闲时直接返回 `200` 和 JSON。其他请求返回 `202`、任务 `id` 和当前状态。查询任务：

```bash
curl 'https://your-server/api/v1/embeddings/jobs/TASK_ID' \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

状态为 `ready` 后，通过返回的 `result_url` 下载 JSON，或通过 `npz_url` 下载 NPZ。两个地址也需要 Bearer Token。

JSON 的 `frames` 数组包含 `frame_no`（从 0 开始）、`time_ms`、`embedding`。NPZ 包含 `frame_no`、`time_ms`、`vectors` 数组及 `metadata.json`。每个向量是归一化的 1024 维 float32 CLS 特征。返回数据包含模型指纹、文件 SHA256、`cache_hit`、损坏帧总数和最多 100 条损坏帧详情。

服务按文件内容和模型指纹复用已有完整向量。缺少向量时使用当前推理设备生成，搜索优先；后台一次处理一个 API 任务。进度保存在 SQLite，应用重启后继续。API 生成的向量和结果保存在 `data/api_embeddings`，不会加入搜索索引。

“清理 API 缓存”会取消 API 任务并移除缓存和上传文件，搜索索引与原媒体保留。关闭服务会停止继续取帧，重新开启后继续；队列最多等待 100 个任务。调用失败返回 `error`，可重新提交。
