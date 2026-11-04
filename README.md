# B站会员购抢票脚本 (Bilibili Ticket Bot)

面向 Windows 的 B 站会员购 H5 购票脚本，可从 Python 运行，也可打包为两个 EXE。

## 当前版本状态

本阶段已完成扫码登录、开售倒计时、选票、提交订单和异常中断保护。34 项离线回归测试、本地 Chrome 模拟下单和 Windows 打包启动检查均已通过。实际扫码和正式开售仍待实测。

正式模式需要可识别的会员购活动详情页。多张票的观演人核对、验证码、排队、额外实名校验和最终付款由用户处理。页面变化或订单结果不明确时，程序会停在可检查的步骤。

## 功能

- **扫码登录**：`BilibiliLogin.exe` 打开浏览器扫码保存 Cookie
- **购票流程**：`BilibiliTicketBot.exe` 等待开售，选择场次、票档、票数，进入确认订单后提交
- **服务器时间同步**：自动校准 B站服务器时间
- **验证码/风控检测**：检测到验证码自动暂停，等你手动完成
- **异常恢复**：正式启动前核对配置，写入预订记录后才点击，重启时拦截同活动的重复提交
- **截图和日志**：记录关键步骤和异常，方便定位页面变化

## 使用方法

### 1. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

使用 Windows 10/11 和 Python 3.10 或更新版本。程序优先使用已安装的 Chrome 或 Edge。如果系统里没有这两个浏览器，可以用 `.\.venv\Scripts\python.exe -m playwright install chromium` 安装 Playwright Chromium。

### 2. 配置 `config.yaml`

```yaml
# 必填
ticket_url: "https://show.bilibili.com/platform/detail.html?id=YOUR_ID"
sale_time: "2026-06-20 18:00:00"
target_date: "2026-07-12"

# 选填
target_ticket: "128"        # 正式模式必填，演练留空时使用第一个可用票档
ticket_count: 1             # 1–4 张
viewer_name: "张三"         # 已添加的观演人姓名，留空或购买多张时手动提交
payment_method: wechat      # 支付方式 wechat / alipay
test_mode: true             # true 在购票弹窗内演练，false 允许提交订单

# 优化参数
action_delay: 0.5           # 操作间隔秒数，越大越像人
sale_timezone: Asia/Shanghai
sale_time_offset: 0.0       # 正数推迟，负数提前
countdown_seconds_before: 60
max_retry: 30               # 含首次提交，最多 30 次
```

日期、时间和活动编号均为示例。先用活动实际信息填写模板，并保持 `test_mode: true` 做一次演练。正式模式下，`sale_time` 按 `sale_timezone` 解释，默认是中国标准时间。

### 3. 运行

```powershell
.\.venv\Scripts\python.exe login.py
.\.venv\Scripts\python.exe ticket_bot.py
```

Windows 打包版从同一个目录运行两个 exe，并将 `config.yaml` 放在该目录。主程序也会在 Cookie 失效时打开扫码登录。

正式模式需要可见浏览器、会员购活动详情链接和明确的票档。启动时会显示活动、开售时区、场次、票档、票数、观演人和支付方式。核对后，按提示输入 `BUY 活动编号`，例如 `BUY 123`。直接回车会退出。填写错误、链接跳转到其他活动、服务器时间无法校准或关键选项无法核对时，程序会停止自动操作。

### 4. 打包为 exe

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --onefile --console --name BilibiliLogin login.py
.\.venv\Scripts\python.exe -m PyInstaller --onefile --console --name BilibiliTicketBot ticket_bot.py
if (-not (Test-Path .\dist\config.yaml)) {
    Copy-Item .\config.yaml .\dist\config.yaml
}
```

两个 EXE 生成在 `dist/`。打包只包含程序，Cookie、日志、截图和待核对订单记录由程序在本机生成。它们可能含有账号或订单信息，分享诊断材料前请检查内容。

## 抢票流程

```
打开页面 → 同步服务器时间 → 等待开售
→ 刷新页面 → 等底部"立即预订"按钮就绪
→ 点击打开弹窗 → 选日期、票档和票数
→ 正式模式下点"立即预订" → 确认观演人和支付方式 → 提交订单
→ 服务端明确拒绝且页面提供可用的"再试一次"时尝试重试
→ 创建成功后由用户完成付款
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `ticket_bot.py` | 主抢票脚本 |
| `login.py` | 扫码登录脚本 |
| `browser_utils.py` | 浏览器检测工具 |
| `order_guard.py` | 正式运行互斥与待核对订单记录 |
| `config.yaml` | 默认以演练模式启动的配置模板 |
| `tests/` | 离线回归测试 |
| `cookies.json` | 登录状态（自动生成） |
| `screenshots/` | 截图文件夹（自动生成） |

## 注意事项

- 订单结果不明确时，脚本会停止自动点击并提示核对订单。最终付款需手动完成
- 多张票需要页面提供可识别的数量控件。正式模式遇到同一天有多个匹配场次、同名观演人或无法核对的支付方式时，会交给用户处理。购买多张时由用户核对所有观演人并手动提交
- 抢票成功率受网络、服务器、票量等多种因素影响
- 仅供个人学习使用

## 开票前自检

在修改脚本、更新 Playwright 或更换电脑后，先运行离线回归测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试不会访问 B 站、不会登录、不会创建订单；它覆盖常见的本地故障和页面变化：

- Windows 缺失 `tzdata`、电脑时区不同、服务器时间同步失败或网络抖动
- 旧的开售时间被误用于正式模式、票数/重试次数/时区配置错误
- Cookie 损坏、Chrome/Edge 或 Playwright 浏览器运行时缺失
- 按钮外层容器被误点、按钮被禁用、底部按钮文案/样式变化
- 无关弹窗被误认为购票弹窗、页面关闭时截图失败
- 拥挤或风控导致下单接口返回失败，却被页面文案误判为成功

使用 `test_mode: true` 可以演练到购票弹窗内的场次、票档和票数选择。观演人及支付方式属于后续订单确认流程。遇到验证码、排队、实名校验或页面新增确认步骤时，可在浏览器中手动处理。

## 中断后恢复

正式模式在点击弹窗内的预订按钮前，将记录写入 `%LOCALAPPDATA%\BilibiliTicketBot\order_state`。同一 Windows 用户下，源码和 EXE 共用这份记录，同一个活动只允许一个正式实例运行。即使程序崩溃、断网或关窗，记录也会保留。跨设备购票时需分别检查订单状态。

重开时若提示有待核对记录，先在会员购订单页确认该活动的订单状态。需要再次购票时，在程序所在目录执行以下命令，将 `123` 换成活动编号，然后按提示输入 `CHECKED 123`。这个操作只解除本地保护。

```powershell
.\BilibiliTicketBot.exe --clear-order-guard 123
# 从源码运行
python ticket_bot.py --clear-order-guard 123
```
