# store/ — 反爬层运行时数据

不入 skill 交付物（本工作区无 git，靠目录约定维护）。

- `profiles/<site>/user_data_dir/` — 每站独立浏览器 profile（cookie/登录态/storage），
  由 scripts/login.py 交互式登录后生成并持久化。删除即"退出登录"。
- `profiles/<site>/profile.json` — 该站固定的指纹元数据（viewport/locale/timezone），
  同 profile 内绝不随机。
- `state/<site>.json` — pacing 计数（日期 + 当日抓取页数），跨天自动归零。
- `state/<site>.login.json` — 最近一次登录记录（login.py 写入，check_login.py 展示）。
- `state/health.json` — check_login.py 的全站健康检查快照。
- `proxy.json` — 代理配置预留位，**默认不存在**；需要时从 `proxy.example.json`
  复制并置 `"enabled": true`。家庭 IP 场景请保持关闭。
