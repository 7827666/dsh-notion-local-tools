# dsh-notion-local-tools

把你自己的 **notion2api 请求地址 + API key** 接入 DSH，让模型通过 DSH 的本地工具读写当前电脑的文件。

默认提供 **Sonnet 5.5** 与 **Opus 5.5**。插件不包含任何人的服务器地址、API key、Notion cookie、私人会话或测试文件。它也不会在远程服务器上安装服务。

## 复制以下内容给 agent 安装

将下面整段复制给你电脑上的编程 agent。请求地址和 API key 留到本机启动向导中输入，不需要写进这段提示词或发到聊天里。

```text
请按以下仓库的 README，在我的电脑上安装并验收 DSH 插件：
https://github.com/7827666/dsh-notion-local-tools

先核对 Node.js、DSH 版本和实际使用的 profile，安装预打包 Release；不兼容时先说明。保留现有配置与默认模型，修改前备份，避免旧版重复注册和中断正在运行的任务。

按 README 启动交互式向导，让我在本机自行输入请求地址和 API key，不要要求我发到聊天里，也不要记录或公开这些信息。

确认能选择“Notion · 本地工具”的 Sonnet 5.5 / Opus 5.5。用独立测试文件，让 Sonnet 通过 DSH 的 read → write → read 完成读写；模型不用 shell。独立核对输入、输出字节一致，包括末尾换行。

最后告诉我安装的 profile、下次启动命令和实际验收结果；未完成的部分如实说明。
```

## 安装

本版已按 **DSH 0.2.0-rc.2 / Node.js 22.22+** 制作；先运行 `dsh --version` 检查版本。不兼容时请使用匹配版本，不要绕过 DSH 的版本检查。

在已安装 DSH 的电脑终端执行：

```bash
dsh plugin --profile web add "https://github.com/7827666/dsh-notion-local-tools/releases/download/v0.2.1/dsh-notion-local-tools-0.2.1.tgz"
```

这条命令安装预打包插件并启用其 DSH bundle，不需要运行本项目构建脚本。使用其他 DSH profile 时，将 `web` 换成实际名称；桌面版请使用其自带的插件安装入口或终端。

从旧版本升级时，先等待正在运行的任务结束并关闭同一 profile 的 DSH 服务，再运行上面的安装命令，随后重新启动向导。新代码需要重启 DSH 才能完整生效；保留原有配置和默认模型，不要重复注册提供方。

也可以从源码安装：

```bash
git clone https://github.com/7827666/dsh-notion-local-tools.git
cd dsh-notion-local-tools
dsh plugin --profile web add .
```

若你以前装过手工路径版 `notion-local-tools-adapter`，先停用该旧条目，避免同一个提供方重复注册。不要覆盖整个 DSH 配置文件。

## 填地址和密钥并启动

本版随包提供交互式启动向导。先关闭正在使用同一 profile 的 DSH Web 服务，再运行：

```bash
dsh plugin --profile web exec dsh-notion-local-tools
```

按提示依次输入 **notion2api 请求地址** 和 **API key**。地址例如 `https://your-gateway.example/v1`，完整的 `/v1/chat/completions` 地址也可以。密钥输入不回显，不放在命令参数或终端历史中。然后浏览器会打开 DSH：选择自己的工作区，在模型菜单选择 **Notion · 本地工具 → Sonnet 5.5 / Opus 5.5** 即可。

**每次启动都使用这条向导命令并重新输入地址和密钥。** 密钥仅用于本次 DSH 进程的环境，不持久写入凭据文件。地址只存于权限受限的临时 overlay，向导在退出时尝试清理；插件管理器中断、强退或断电可能留下只含地址的临时文件（系统临时目录中的 `dsh-notion-local-tools-*`），可在停止服务后删除。使用其他 profile 时，内外两处名称都要替换，例如 `dsh plugin --profile custom exec dsh-notion-local-tools --dsh-profile custom`。需要改端口时，在命令末尾添加 `--port 3085`。不要一边运行旧服务，一边启动同端口的新服务。`dsh plugin exec` 在运行期间会持有该 profile 的插件管理锁；安装、升级或卸载其他插件前，先按 Ctrl+C 停止向导服务。

DSH 0.2.0-rc.2 的“设置 → 模型”能显示第三方提供商，但其编辑表单只支持内置提供商；因此本版通过向导配置，不要求在该表单填写密钥。插件不会替你切换默认模型、放开文件权限或取消审批。工作目录、文件权限与审批仍由 DSH 管理。旧手工路径版需要先停用，避免同名提供方重复注册。

如果 CLI 安装位置特殊、向导找不到 DSH，可将 `DSH_CLI_ENTRY` 指向已安装 DSH 的 `lib/bin.js` 再启动。该入口使用本机 Node 启动 DSH，不通过 shell 拼接命令。

**分享方式：**可以把这个 GitHub 链接直接给朋友；请求地址和 API key 通过你们自己的私密渠道分别告知，不要提交到 Issue、截图或公开仓库。HTTPS 地址会加密传输；如果你们使用已有 HTTP 地址，密钥和提交的文件内容会以明文经过该连接。

## 自己验收一次

在工作区新建一个只含测试文字的 `input.txt`，例如自己生成的随机词，然后对 DSH 说：

> 只操作这个工作区的 input.txt 和新的 output.txt。先使用 read 读取 input.txt，再使用 write 将读取到的内容写入 output.txt（保留原文件的末尾换行），最后用 read 回读确认。不要用 shell，不要操作 Notion 页面或其他文件。

检查 DSH 出现真实的 `read → write → read` 轨迹，并在本机打开 `output.txt` 核对。不要只凭模型说“已保存”就判断成功。

模型与工具结果会通过你配置的 notion2api 发给其上游 Notion；只选你愿意经该服务处理的文件。插件没有直接执行文件或 shell 的代码，执行者是本机 DSH。

## 工作原理和限制

DSH 的工具定义、文本历史和实际工具结果被编码为文本，发送到配置的 notion2api。模型返回结构化 JSON 工具决策，插件将其转换为 DSH 原生工具调用。只接受完整 JSON 与本轮已声明的工具名，不从普通回复中猜测操作。

- 针对文本文件与兼容 `createAgentThread` 文字桥接的 notion2api 制作；不是通用的原生 function calling 网关。
- 默认两个模型已在原适配中验证；其他模型即使出现在服务列表里，也需要单独验证结构化输出和工具循环。
- 不支持图片输入。文件操作不是逐 token 输出；每轮携带工具说明和必要历史，会增加输入与延迟。
- 上游认证失效、502、轮询超时仍可能失败。插件保留 DSH 的错误分类与取消，不承诺所有服务或模型长期稳定。
- DSH 的会话标题在本地截取首条消息生成，不额外调用 Notion；正常工具调用仍由 DSH 执行。
- v0.2.0 包含异步长任务、查询恢复和文本工具协议 v2；无需改用其他反代产品。
- 支持异步任务的服务端会先返回任务编号；客户端只提交一次，再用 GET 查询同一个任务，不依赖一条持续数分钟的 HTTP 连接。查询临时断线或返回通用 502/503/504 时，在同一总预算内退避重试 GET，间隔最多 30 秒；明确的任务/认证错误、取消或总期限会停止等待，不重发生成请求。
- 每轮默认总等待 31 分钟，可通过 `timeoutMs` 配置覆盖（1000–3600000 毫秒）；配套服务端默认最多等待 30 分钟。旧服务端仍可返回原来的同步响应，其自身超时不会因客户端配置而改变。
- 此提供商禁用 DSH 自动重发。取消或超时只停止本机等待，上游任务可能仍在运行；不要重复提交。多轮工具调用仍各有一次模型推理，同账号并发与结果缓存由服务端负责。
- 配套 v2 服务端在会话、模型、工具和历史前缀一致时，通过 `sendEventToAgentThread` 继续同一个 Notion 线程，只发送新增消息。历史编辑、裁剪、系统指令或工具定义变化会安全新建线程；不能承诺所有任务始终只有一个线程。
- 后端核对本轮消息 ID、committed 完成事件及 idle 状态，避免把上一轮结果当作本轮答复。客户端核对服务端提供的任务 ID，并稳定派生工具调用 ID；这不等于保证工具恰好执行一次。
- 同线程优化需要服务端配套更新；旧服务仍可按完整文本请求处理。运维代码与集成边界见 [server/README.md](server/README.md)，普通使用者只安装客户端。
- 本地工具指令是文本约定，不是 Notion 原生工具的硬禁用开关。只有 DSH 的实际工具轨迹和本机文件才是本地读写凭据；`/data/...` 和 `session-file://...` 不能证明文件已保存到你的电脑。
- 源码使用跨平台 Node.js API；实际 DSH 集成验收以发行说明列出的系统为准，不能把单元测试等同于另一台电脑的现场验证。

## 停用 / 卸载

```bash
dsh plugin --profile web remove dsh-notion-local-tools
```

不会删除你的业务文件。需要继续使用的旧会话先切到其他模型。凭据由 DSH 单独管理，如不再使用，请通过原先保存该凭据的入口移除相应凭据；本向导不保存密钥。

## 开发

```bash
npm ci --ignore-scripts --legacy-peer-deps
npm run check
npm pack --ignore-scripts
```

`--legacy-peer-deps` 仅用于本项目独立开发测试；正常用户通过 DSH 安装，不要用它绕过 DSH 的运行时版本检查。本项目不提供安装或构建生命周期脚本。MIT License。

协议依据为独立网页验证；设计参考了 [Sub2API](https://github.com/Wei-Shaw/sub2api) 的消息关联处理和 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的工具 ID 管理。未复制 Sub2API 的 LGPL 代码，也未引入自动重放未知生成请求或切换账号的策略。
