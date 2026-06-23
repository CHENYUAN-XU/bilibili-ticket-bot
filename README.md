# B站会员购抢票脚本 (Bilibili Ticket Bot)

自动抢 B站会员购 H5 网页票务的 Python 脚本，打包为 Windows 一键 exe。

## 功能

- **扫码登录**：`BilibiliLogin.exe` 打开浏览器扫码保存 Cookie
- **自动抢票**：`BilibiliTicketBot.exe` 从开售前倒计时到提交订单全自动
- **服务器时间同步**：自动校准 B站服务器时间
- **验证码/风控检测**：检测到验证码自动暂停，等你手动完成
- **手动兜底**：按钮找不到时提示你手动点击后继续
- **截图留存**：每一步自动截图，方便复盘

## 使用方法

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置 `config.yaml`

```yaml
# 必填
ticket_url: "https://show.bilibili.com/platform/detail.html?id=YOUR_ID"
sale_time: "2026-06-20 18:00:00"
target_date: "2026-07-12"

# 选填（不填则脚本启动时询问）
target_ticket: "128"        # 票档关键词如 "128" / "VIP票" / "游园票"
viewer_name: "张三"         # 观演人姓名
payment_method: wechat      # 支付方式 wechat / alipay
test_mode: true             # 测试模式 true / 正式抢票 false

# 优化参数
action_delay: 0.5           # 操作间隔秒数，越大越像人
sale_time_offset: 0.0       # 时间偏移秒数，本地快就填正数
countdown_seconds_before: 60
max_retry: 30
```

### 3. 运行

```bash
python ticket_bot.py
```

### 4. 打包为 exe

```bash
python -m pip install pyinstaller
python -m PyInstaller --onefile --console --name BilibiliLogin login.py
python -m PyInstaller --onefile --console --name BilibiliTicketBot ticket_bot.py
# exe 在 dist/ 目录下
```

## 抢票流程

```
打开页面 → 同步服务器时间 → 等待开售
→ 刷新页面 → 等底部"立即预订"按钮就绪
→ 点击打开弹窗 → 选日期 → 选票档
→ 点"立即预订" → 确认订单页提交
→ 失败自动点"再试一次"重试
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `ticket_bot.py` | 主抢票脚本 |
| `login.py` | 扫码登录脚本 |
| `browser_utils.py` | 浏览器检测工具 |
| `config.yaml` | 配置文件 |
| `cookies.json` | 登录状态（自动生成） |
| `screenshots/` | 截图文件夹（自动生成） |

## 注意事项

- 需要 Windows 10/11 且安装了 Chrome 或 Edge
- 仅自动化到支付页面，最终付款需手动完成
- 抢票成功率受网络、服务器、票量等多种因素影响
- 仅供个人学习使用
