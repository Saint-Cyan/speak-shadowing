# 口语跟读 Speak Shadowing

面向中国工程师的英语口语练习工具：把一段英文、一个工作场景，或一段 B 站 / 本地音视频，变成可以逐句听、跟读、录音对比、让 AI 点评的对话材料。

单页网页应用（PWA），手机和电脑都能用，可“添加到主屏幕”。

## 功能

- **朗读与跟读**：逐句朗读（edge-tts，美 / 英 / 澳 / 新西兰多种口音）、逐词高亮、变速 0.5–1.5×、每句重复、句间停顿、单句循环；「🎯 跟读」先播原音再自动录音、然后回放对比。
- **话题 → 对话 → 句子**：登录后长期保存、多设备同步；A/B 两种声音；「我演 A / 我演 B」角色扮演（隐藏自己的台词、留出时间说）。
- **AI**（模型可选）：按场景生成整段对话、按意见修改整段、单句改写、单句点评（地不地道、怎么说更好）。
- **难词与生词本**：自动标出每句的难词和特殊用法（音标、词性、本句释义）；点任意单词可听单词 / 慢速 / 句中读音；生词本跟着对话走。
- **历史录音与发音分析**：每句的每次录音都保留；可让 AI 听录音，指出问题词并给出针对性练习。
- **音视频导入**：粘贴 B 站链接（可多选分 P、选时间段）或上传本地音视频，自动转写、分说话人、配中文翻译，按时长切成若干段对话，每句可播放原声。
- **管理员统计**：所有用户的 AI 调用次数、按功能 / 模型 / 用户统计。

## 架构

```
浏览器（static/index.html，单文件页面）
  ├─ 云服务 SDK：登录、数据库（PostgreSQL + 行级安全）、文件存储、大模型
  └─ 本项目后端（FastAPI）
       ├─ /api/tts            edge-tts 合成，返回音频与逐词时间轴
       ├─ /api/media/*        B 站解析与下载、本地文件上传，ffmpeg 转码并按停顿切段
       ├─ /config.js          把公开配置下发给页面
       └─ /                   静态页面
```

| 文件 | 作用 |
|---|---|
| `app.py` | 后端入口：语音合成、静态页面、配置下发 |
| `media.py` | 音视频导入：B 站接口、上传、ffmpeg 切段、登录核实 |
| `config.py` / `config.example.json` | 站点配置（环境变量优先，其次 `config.json`） |
| `static/index.html` | 全部前端逻辑 |
| `db/schema.sql` | 数据库表结构、函数、行级安全策略 |

## 当前依赖说明（请先读）

目前登录、数据库、文件存储、大模型都依赖 [WorkBuddy](https://www.workbuddy.cn) 的云服务（接口与 Supabase 类似）：

- 大模型由浏览器免密钥调用，不需要自己的 API Key，但只能在 WorkBuddy 托管的域名下使用。
- 「听音频」（发音分析、音视频转写）目前只有 Gemini 系列可用，且走的是把音频放进图片字段的非官方用法，单次约 60 秒上限，随时可能失效。
- 后端（语音合成、B 站下载、切段）不依赖 WorkBuddy，任何能装 ffmpeg 的服务器都能跑。

计划中的改造：AI 改为经本项目后端转发（自带 OpenAI 兼容接口 / Gemini 官方接口的密钥），数据与登录可切换到 Supabase。

## 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # 填入你的云服务地址和公开配置
PORT=8765 python app.py
# 打开 http://localhost:8765
```

不填 `config.json` 也能启动：朗读、跟读、本地录音可用；登录、话题、AI 不可用。

ffmpeg：优先用系统里的 `ffmpeg`，没有时使用 `imageio-ffmpeg` 自带的版本。

## 配置项

| config.json | 环境变量 | 说明 |
|---|---|---|
| `cloud_endpoint` | `SPEAK_CLOUD_ENDPOINT` | 云服务地址（WorkBuddy 托管时就是站点域名） |
| `cloud_publishable_key` | `SPEAK_CLOUD_PUBLISHABLE_KEY` | 云服务公开配置（`wbpk_` 开头，会出现在浏览器里，不是密钥） |
| `oauth_relay_url` | `SPEAK_OAUTH_RELAY_URL` | 微信等第三方登录的中转地址 |
| `share_code` | `SPEAK_SHARE_CODE` | 托管网关的访问码，用于“添加到主屏幕”的启动地址（可选） |
| — | `PORT` | 监听端口，默认 3000 |
| — | `MEDIA_DIR` | 导入时的临时目录，默认 `/tmp/speak-media` |

## 部署

**Docker**

```bash
docker build -t speak-shadowing .
docker run -p 3000:3000 -e SPEAK_CLOUD_ENDPOINT=... -e SPEAK_CLOUD_PUBLISHABLE_KEY=... speak-shadowing
```

**WorkBuddy 托管**：把项目目录作为 Python 应用发布，安装命令 `pip install -r requirements.txt`，启动命令 `python app.py`，并在目录里放好 `config.json`。

**数据库**：在云服务的 PostgreSQL 里执行 `db/schema.sql`，然后插入一条管理员认领码，在页面右上角 ⚙︎ →「管理员认领」里使用：

```sql
INSERT INTO admin_claim_codes(code) VALUES ('你自己的随机字符串');
```

## 说明

- 音视频导入仅供个人学习使用，请遵守视频平台的用户协议和版权规定；会员专享或已下架的内容无法导入。
- AI 生成的释义、音标、发音评分仅供参考。

## License

[MIT](LICENSE)
