# 模型中转站 · 本地工作台

在 Windows 上运行的单用户工作台。界面可点选、拖动、增删与暂停流程节点；每个节点可设置执行工具、模型 ID、系统指令、输出 token 上限和请求超时。当前按从左到右的顺序执行，后一节点接收前一节点的文本。没有上传网站，也没有替你购买或创建任何 API Key。

## 安全与范围

请勿提交 `state.json`、`secrets.dpapi.json`、缓存或真实任务内容。本仓库不含密钥；运行前由用户自行配置。项目按现状提供，内置演示题不证明多模型整体优于单模型。

## 启动

只依赖 Python 3.10+ 标准库：

```powershell
Set-Location '你克隆的 local-ai-workbench 目录'
python server.py
```

打开 <http://127.0.0.1:8765>。点击 **载入可运行模板**，会在当前画布中载入 DeepSeek API → Pi CLI → DeepSeek Harness 的三步流程；点击“运行流程”时自动保存。原有流程如已保存，重新载入页面可恢复。该模板会调用真实模型，可能产生费用。

## 集群对比实验

在“发起任务”填写同一道题，再在“集群对比实验”写清验收标准，点击“与单模型对比”。工作台先得到 DeepSeek 单独作答，再让 Pi、Harness 和 Hermes 独立作答，由 DeepSeek 综合成功的候选。Hermes 失败时仍可使用另外两个候选。最后随机交换两份答案的 A/B 位置，用 DeepSeek 按验收标准审查。两份都通过硬性要求时，只有审查判定集群分数严格高于基线才选择集群；若单模型违反硬性要求、集群通过，则选择集群；集群违反硬性要求、平局、审查失败或合成失败时保留单模型答案。

页面保留两份答案、独立候选、审查理由、耗时和已知 API token。对于明确写出的“最多/不超过 N 字”“3 个编号步骤”“失败回退规则”，程序先做硬性检查；集群答案不合格时尝试修订一次，仍不合格就保留单模型答案。字数按非空白字符计算。CLI 的 token 用量尚不可见，所以不能把“已知 API 用量”当作总成本。模型自审只是一次估计，**不保证集群对任意任务都更准确**。要证明长期更优，需要固定题集、可检查的评分标准，并持续比较胜率、费用与耗时。

## 编程与排错实测及群聊视图

点击“运行编程实测”，工作台向 DeepSeek 单模型、Pi、DeepSeek Harness 与可用的 Hermes 发同一道 `first_pair` 缺陷修复题。DeepSeek 合成独立答案；受限代码执行器对每份答案运行 11 个固定用例，必要时把失败用例交给 DeepSeek 修订一次。集群最佳候选的通过数**严格高于**单模型时才采用它；持平时保留单模型。代码超出执行器允许的 Python 语法时显示“未评分”，单模型未评分时不会宣称集群胜出。测试只覆盖这一道公开题，不构成总体能力证明。

流程执行、模型对比和编程实测都用群聊方式呈现。并行候选在请求完成后按协作阶段显示，页面不会把它们伪装成实时逐条返回。代码在单独进程中运行，限制语法、可调用函数和执行时间；这只适用于内置的短函数题，不应直接用于运行任意仓库代码。

API 节点在“服务商与密钥”中分别录入密钥。连通测试会发起真实 API 请求，可能产生少量费用。每家服务商的密钥只交给其官方域名；其他 URL 会被服务端拒绝。

Pi 节点调用本机 `~/.pi/agent/bin/pi.cmd`，使用非交互文本模式，并禁用工具、扩展、MCP、技能和会话保存；任务经标准输入送入，避免出现在命令行参数中。DeepSeek Harness 节点调用桌面版自带的 `dsh.cmd headless -`，任务也经标准输入送入。Harness 自身可能仍有内置工具，工作台把它的工作目录设为系统临时目录，并在指令中要求只返回文本；这不是操作系统级沙箱。两种 CLI 的模型订阅或本机登录由各自应用管理，工作台不读取其凭证。

在原开发环境中，Hermes 曾以 `deepseek-flash` 完成一次真实文本请求；新机器需单独验证。它原先配置的默认模型名与 DeepSeek 接口不匹配，所以工作台节点默认显式指定 `deepseek-flash`。Hermes 使用 `--query-file -` 接收标准输入，空工具集、最多 1 个工具迭代，作为可选自动节点；它的账号和模型配置仍由 Hermes 自己管理。

Kimi 桌面版和豆包工作版提供 **人工交接节点**：流程暂停并给出可复制的任务；在桌面应用取得回复后贴回工作台，流程继续。桌面应用登录状态不等于 API 密钥。

密钥以 Windows 当前用户 DPAPI 加密后保存到 `secrets.dpapi.json`，页面只显示是否已配置，不显示密钥内容。也可以通过 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`MOONSHOT_API_KEY`、`ZHIPUAI_API_KEY`、`ARK_API_KEY` 环境变量提供；环境变量优先。`state.json` 只存流程设置，不存密钥。两者已加入忽略列表。不要把本地服务暴露到公网。

## 给 Codex 增加工作台工具

你可以把 `local_ai_workbench` 注册为 Codex 的用户级 MCP 工具。重新启动 Codex 后即可发现它。配置仅让 Codex 调用你在界面中保存的流程，不会切换 Codex 自身的模型。不要把 API Key 写进 Codex 配置文件。

若在另一台电脑使用，可在用户级 `~/.codex/config.toml` 中增加以下配置，**把路径改成实际绝对路径**：

```toml
[mcp_servers.local_ai_workbench]
command = "python"
args = ["C:/path/to/local-ai-workbench/mcp_bridge.py"]
```

Codex 可调用 `provider_status`、`run_workflow`、`resume_workflow` 和 `compare_workflow`。`run_workflow` 会在人工交接节点返回 `run_id` 与待复制任务，`resume_workflow` 接收用户贴回的结果；暂停状态仅保存在本机服务的内存中，一小时后过期，重启服务后需重新运行。Codex 仍可做最终审查。这个 MCP 桥接使用标准输入输出并访问本机服务；浏览器可以关闭，但 `python server.py` 必须保持运行。

## 当前范围

- 已接入 DeepSeek、通义千问、Kimi、智谱 GLM、火山方舟豆包的文本 Chat Completions API。模型 ID 可在界面中按账号权限更改，豆包可能需要填你开通的推理接入点 ID。
- 目前是顺序编排。Pi 作为禁用工具的文本节点；Harness 的 Headless 模式可能使用自己的工具，请仅给它适合在本机运行的任务。Hermes 作为空工具集文本节点。界面上的 Temperature 对通义千问和 GLM 生效；对其余服务商与本机 CLI 不发送。
- 没有配置真实密钥时，只能验证本地界面、流程和请求格式，不能声称远程 API 已连通。
- 暂不自动计算跨平台费用。界面显示服务商返回的 token 用量；价格与缓存规则应按各平台的当前账单核对。

## 官方接口依据

- [DeepSeek](https://api-docs.deepseek.com/guides/your_first_api_call)
- [阿里云百炼](https://help.aliyun.com/zh/model-studio/base-url)
- [Kimi](https://platform.kimi.com/docs/get-api-key)
- [智谱](https://docs.bigmodel.cn/)
- [火山方舟](https://docs.volcengine.com/docs/ark/base-url-and-authentication)
- [DeepSeek Harness Desktop CLI](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)
- [Kimi Code Desktop 与 CLI 的区别](https://www.kimi.com/code/docs/en/kimi-code-desktop/getting-started.html)
- [Hermes Agent 文档](https://hermes-agent.nousresearch.com/docs)
