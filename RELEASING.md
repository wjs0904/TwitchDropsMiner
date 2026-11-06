# 发版流程

本仓库沿用上游的 GitHub Actions 工作流（`.github/workflows/ci.yml`），
只额外增加了「分支构建」与「标签发版」两个触发点。

## 一次性准备

1. 在 GitHub 上创建仓库（空仓库即可，不要勾选 README / .gitignore 初始化）。
2. 本地已有 6+1 个提交在 `feature/multi-account` 分支上，绑定到你的仓库：

```bash
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin feature/multi-account
```

3. 仓库需要 Actions 写入权限才能发 Release：
   **Settings → Actions → General → Workflow permissions**
   选择 `Read and write permissions`，保存。

## 日常构建

推送到 `master` / `main` / `feature/multi-account` 任一分支，都会触发：

- `validate`：校验 `lang/*.json` 全部合法
- `windows` / `macos` / `linux-pyinstaller` / `linux-appimage`：四路构建
- 产物作为 Actions artifact 上传，可在运行页面下载

## 正式发版（自动）

打标签即发版：

```bash
git tag -a v16.0 -m "Multi-account release"
git push origin v16.0
```

触发 `publish_release` 作业，把本次构建的四份产物作为非预发布 Release 发布，
标题取 `version.py` 里的版本号。若同名 tag 已有 Release，会原地更新（`allowUpdates`）。

## 预发布（dev-build）

非标签的 `master` 推送会走 `update_releases_page`，生成/覆盖 `dev-build` 这个
预发布条目，行为与上游完全一致。

## 版本号

版本号来自 `version.py` 的 `__version__`（当前 `16.dev`）。
CI 会在构建时把短 SHA 追加进去（`16.dev.2b416d4`），但 `publish_release` 读的是
检出后的源码，所以 Release 标题用的是干净的 `16.dev`。要发 `v16.0` 这类正式版，
先把 `version.py` 改成对应值再打标签。
