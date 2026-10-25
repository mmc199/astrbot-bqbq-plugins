# AstrBot 表情包插件合集

适配 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的一组插件，配合
[bqbq_v2](https://github.com/mmc199/bqbq_v2) 表情包后端使用。

## 插件列表

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_bqbq_collect` | 表情包收集：自动收取群消息与合并转发里的表情，转存到 bqbq_v2 后端（分片存储、生成缩略图、同步 R2/CDN）；并提供搜索、看图、打标签等命令 |
| `astrbot_plugin_bqbq_probe_single` | 消息结构探测：把收到的消息组件、引用链、原始 payload 写入 jsonl，便于排查适配器问题 |
| `astrbot_plugin_resolver2` | 多平台链接解析：B站 / 抖音 / 快手 / 小红书 / YouTube 的分享链接自动解析并发送 |

## bqbq_collect 快速上手

### 1. 部署后端

跑 [bqbq_v2](https://github.com/mmc199/bqbq_v2)，默认监听 `http://127.0.0.1:8000`。

### 2. 配置插件

在 AstrBot 的插件配置里填：

| 配置项 | 说明 |
|---|---|
| `backend_url` | 后端地址，如 `http://127.0.0.1:8000` |
| `bot_client_id` | **bot 自身凭证**，见下方说明 |

### 3. 配置 bot 凭证（必填）

后端写接口（上传图片、修改标签）需要鉴权。bot 用的是一个长期有效的
`client_id`，格式建议 `bot_astrbot_` + 随机字符。

**生成并登记**：

```bash
# 生成一个（13 位足够）
python -c "import secrets; print('bot_astrbot_' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz23456789') for _ in range(13)))"
```

把它同时写到两个地方：

1. AstrBot 插件配置的 `bot_client_id`
2. 后端数据库：

```sql
INSERT INTO auth_granted_clients (client_id, openid, kind)
VALUES ('bot_astrbot_你的随机串', '__bot_astrbot__', 'bot');
```

> 仓库里不包含任何真实凭证，必须自己生成。

### 4. 命令

格式统一：**`bqbq` + 空格 + 内容**。

| 命令 | 说明 |
|---|---|
| `1 3` | 看第 1、3 张原图（编号见预览图） |
| `bqbq 猫` | 搜索（自动展开同义词） |
| `bqbq 猫 可爱` | 多个词 = 同时满足 |
| `bqbq add 可爱` | 追加标签 ⚠️ 需先引用图片 |
| `bqbq tag 猫 可爱` | 替换全部标签 ⚠️ 需先引用图片 |
| `bqbq del 可爱` | 删掉指定标签 ⚠️ 需先引用图片 |
| `bqbq del` | 整张丢进回收站 ⚠️ 需先引用图片 |
| `bqbq 帮助` | 查看全部命令 |
| `bqbq 统计` / `bqbq 重置` | 收集数量 / 清零 |
| `bqbq 绑定` | 查看自己的 openid |
| `bqbq 白名单` | 查看私聊白名单 |
| `bqbq 登录 <4位码>` | 网页登录 |
| `bqbq 取回` | 取购物车里的表情包 |

**关于引用**：`add` / `tag` / `del` 只有在**真正引用了一张图片**时才生效。
没引用图片时，这些词会被当普通搜索词处理。

预览图上动图的编号是**橙色**，静图是黑色。

## 目录结构

```
astrbot_plugin_bqbq_collect/
    main.py          收集主逻辑 + 命令
    imagelib.py      后端接口封装（搜索/上传/查重/取图/拼图）
    commands.py      命令解析与分发
    metadata.yaml    插件元信息
    _conf_schema.json 配置项定义

astrbot_plugin_bqbq_probe_single/
    main.py          消息结构探测器

astrbot_plugin_resolver2/
    main.py          解析主逻辑
    parsers/         各平台解析器
    *登录.py          Cookie 抓取工具（本地运行，不上传）
```

## 隐私说明

本仓库**不包含**任何：

- openid / QQ 号 / 用户标识
- bot 凭证 / token / 密钥
- Cookie 或登录态
- 真实服务器地址（只有 `127.0.0.1` 示例）

`_conf_schema.json` 的 `private_whitelist` 默认为空，需要自己按
`bqbq 绑定` 拿到的 openid 填写。

`resolver2` 的三个登录脚本会把抓到的 Cookie 写进 AstrBot 的
`data/config/` 目录，**不会**写进本仓库。

## 许可

各插件版权归原作者。`resolver2` 请参考其目录内的说明。
