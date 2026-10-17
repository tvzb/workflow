# 电视直播源自动采集、测速与优选工作流

这是一个基于 **GitHub Actions** 的电视直播源采集、IPv4 清洗、连通性探测、测速优选与持久化发布的开源解决方案。

## 核心特性
- **纯手动运行**：仅配置 `workflow_dispatch`，完全由你在 GitHub 界面上手动按需触发，不浪费额度。
- **历史记录安全继承 (防清空机制)**：每次运行前自动读取既有的 `live.txt`。若当次搜索受阻或网络波动，原有的高速可用源自动继承保留，绝不会生成空文件。
- **精选频道覆盖**：
  - **央视频道**：严格保留 `CCTV-1 综合` 至 `CCTV-17 农业农村`（排除 CCTV-5+）。
  - **卫视频道**：涵盖省级卫视、直辖市卫视以及深圳卫视，纯净名称后无任何后缀。
- **科学定制排序**：
  - 卫视频道优先置顶前 7 位：`江西卫视`、`东方卫视`、`北京卫视`、`江苏卫视`、`浙江卫视`、`湖南卫视`、`辽宁卫视`。
  - 剩余各省卫视频道严格按拼音首字母（A-Z）规范排序。
- **高精度真实测速**：
  - 实际下载 TS 媒体切片测算真实下载速率，每个频道按速度降序排列，仅保留最快的 **Top 10** 高清直播源。
- **纯净单文件交付**：输出电视盒子（TVBox、DIYP 影音等）通用的 `live.txt` 格式。

---

## 部署与使用步骤

### 1. 仓库权限配置 (必须设置)
在 GitHub 仓库主页依次点击：
`Settings` -> `Actions` -> `General` -> 找到 **Workflow permissions** -> 选择 **Read and write permissions** -> 点击 `Save` 保存。  
*(此权限用于允许 Action 将生成好的 `live.txt` 自动推送回你的仓库)*。

### 2. 手动运行工作流
1. 打开仓库上方的 **Actions** 标签页。
2. 在左侧选择 **IPTV Live Stream Manual Updater**。
3. 点击右侧 **Run workflow** 下拉按钮，点击绿色的 **Run workflow** 按钮启动运行。
4. 执行完成后，根目录下会自动生成或更新 `live.txt`。

### 3. 播放器订阅使用
你可以直接使用 GitHub Raw 链接添加到 TVBox / DIYP 影音等播放器：
```text
[https://raw.githubusercontent.com/](https://raw.githubusercontent.com/)<你的用户名>/<你的仓库名>/main/live.txt
