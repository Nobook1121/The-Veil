# 第三方依赖许可证清单

本文件由 `scripts` 侧的许可收集流程生成，列出「帷幕 The Veil」运行与构建所使用第三方依赖的
名称、版本与许可证标识，供分发时合规参考。许可证标识取自各发行包自身的元数据；
如需完整许可证文本，请参见对应上游包（Python 包可在 `pip show <包名>`，
Node 包可在 `node_modules/<包名>/LICENSE` 查看）。

## 实时沉浸系统（v0.5.5b）

本次未新增打包依赖：实时配乐使用浏览器内置 Web Audio API 现场合成，实时配图默认使用
本地确定性 SVG（Python 标准库 `hashlib` / `random` / `html`）。可选的外部生图能力复用项目
已有的 `requests` 与 `agents/http_retry.post_with_retry`，仅访问管理员自行配置的 OpenAI 兼容
`/v1/images/generations` 端点，不随项目分发任何模型、音频或图片素材。上游服务的许可证与
计费由部署方自行确认。

## 生图模型平台与 AI 音乐导演（v0.5.6b）

本次同样**未新增打包依赖**。

- 「生图模型平台」只是把已有 OpenAI 兼容生图配置改为可复用的多条平台表，仍复用 `requests`
  与 `post_with_retry`；预设中的厂商名称仅用于填充 Base URL/模型，不打包任何厂商 SDK。
- 「AI 音乐导演」通过项目已有的 OpenAI 兼容 Chat Completions 客户端调用大模型，输出 JSON
  乐谱参数；前端仍由浏览器 Web Audio API 合成，不引入任何音频素材或音乐生成 SDK。
- **未引入外部音乐生成模型**（Suno、Udio、Stable Audio 等）。这类服务的商用授权、计费与
  输出归属存在不确定性，且预生成音频会带来额外延迟与缓存体积。若部署方自行接入，需自行
  确认许可证与条款，并保证异步预生成不阻塞实时播放。
- 所有上游模型调用均由部署方自行配置密钥并承担计费；本仓库不附带密钥、模型权重或生成素材。

## 沉浸面板布局与右侧栏收起（v0.5.8b）

本次**未新增打包依赖**。布局、滚动与收起/展开仅使用现有 React、CSS 与浏览器 API
（`localStorage`、`matchMedia`、`prefers-reduced-motion`），未引入任何 UI、拖拽或动画库。
沉浸面板继续由现有前端模块创建与移动节点，不涉及新的第三方运行时。

## 配乐引擎升级与配乐 Skill（v0.5.13a）

本次**未新增任何打包依赖，也未引入外部音乐生成模型**（Suno、Udio、Stable Audio 等）。
作曲逻辑全部为项目自有代码，运行在浏览器内置的 Web Audio API 上：

- 新增前端模块 `frontend/src/app/immersive/music-theory.ts`、`music-arranger.ts`、`music-voices.ts`，
  重写 `music-engine.ts`；仅使用 `OscillatorNode`、`BiquadFilterNode`、`ConvolverNode`、
  `StereoPannerNode`、`DelayNode`、`DynamicsCompressorNode` 等浏览器标准接口。混响脉冲响应由代码
  现场生成（合成早期反射 + 指数衰减），不加载任何采样音频。
- 动机、和弦进行、编配配方均为项目原创的音程/节奏组合；**不包含、不复制任何受版权保护的乐谱、
  音频、歌词或具体作品旋律**。
- 新增文档型 Skill `skills/film-scoring/`（`SKILL.md` 与 `references/*.md`），只提炼公开的音乐学
  概念与技法名称（pedal point、ostinato、tintinnabuli、微复调、平行五度、半减七、那不勒斯 bII 等），
  不随项目分发乐谱或音频素材。调研参考的手法是公有的作曲技法，不构成对具体作品的复制：

  | 参考方向 | 用途 |
  | --- | --- |
  | Bernard Herrmann 的 pedal point / 固定低音 ostinato / 集群和弦 | 恐怖档位的和声与低音写法 |
  | Hans Zimmer 的分层 ostinato / 分阶段声部进入 | 紧迫与敬畏档位的密度与配器 |
  | Arvo Pärt 的 tintinnabuli 双声写法 | 余韵档位的持续音与钟声 |
  | Ligeti 的微复调 | 多声部同音高集合、不同节奏的卡农 |
  | Debussy / Ravel 的平行五度与色彩和声 | 非功能性和声与「空」和声 |
  | Jóhann Jóhannsson / Ryuichi Sakamoto 的慢速管弦与留白 | 铺垫/余韵的动态曲线 |
  | Ennio Morricone 的打击乐语法与合唱进入 | 定音鼓心跳、合唱进入时机 |
  | 恐怖配乐通用语法（tritone、locrian、bII、cluster、silence as accent） | 反模式清单与和弦色彩表 |

  以上仅作为**技法来源**引用，相关作品、乐谱与录音的版权归各自权利人所有；本项目不复制、
  不改编、不分发这些素材。详细来源与工程转译见 `skills/film-scoring/references/reference-analysis.md`。
  若后续需要引入第三方音频采样或音乐生成服务，必须在此登记许可证并确认商用授权，且不得阻塞实时播放。
- 新增 `backend/trpg_server/immersive/auto_score.py` 为纯 Python 本地作曲器（仅用标准库
  `hashlib`），按档位生成确定性乐谱；不调用任何外部模型或网络服务，也不分发音频素材。

## 本地多音色语音引擎（v0.5.13c）

本次为可选的本地 TTS 新增运行时依赖，权重不随项目分发、也不进入 Git 或发布包：

| 组件 | 版本 | 许可证 | 来源 |
| --- | --- | --- | --- |
| sherpa-onnx | 2.0.1 | Apache-2.0 | https://github.com/k2-fsa/sherpa-onnx |
| onnxruntime（sherpa-onnx 依赖的推理后端） | 由 wheel 带入 | MIT | https://github.com/microsoft/onnxruntime |
| numpy | 2.5.4 | BSD-3-Clause | https://numpy.org/ |
| Kokoro-82M-v1.1-zh int8 权重 | `kokoro-int8-multi-lang-v1_1` | Apache-2.0 | https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh |
| 导出/打包仓库 | `csukuangfj/kokoro-int8-multi-lang-v1_1` | Apache-2.0（继承上游权重） | https://huggingface.co/csukuangfj/kokoro-int8-multi-lang-v1_1 |
| espeak-ng 音素化数据（随权重包提供） | 同包 | Apache-2.0 | https://github.com/espeak-ng/espeak-ng |

- 安装位置：`data/runtime/voice/models/<模型>/`；安装时会写入 `INSTALL.json`（文件大小清单）
  与 `LICENSE.txt`（许可证文本摘要），便于离线审计与再分发合规。
- **明确排除**：AISHELL-3 的 VITS 多说话人导出权重虽然数据集为 Apache-2.0，但导出模型采样率仅
  8kHz，音质与年龄区分不达标，因此不采用；任何 CC-BY-NC、CC-BY-NC-SA 或来源不明的中文权重
  一律不下载、不引用。AISHELL-3 数据集本身的信息保留在选型文档中，仅用于说明排除理由。
- 部署方若自行替换为其它模型，必须确认「允许商用 + 允许再分发」，并在本文件补充登记。

## 开发参考 Skill（Oil UI，2026-11-04）

本次新增一份第三方 Skill，原样安装到 `skills/`，**仅作为开发期参考文档，不进入发布包，也不改变运行时行为**，因此**无新增打包依赖**：

| Skill | 安装路径 | 上游仓库 | 版本 / 提交 | 许可证 |
| --- | --- | --- | --- | --- |
| Oil UI（界面设计 / 评审 Skill） | `skills/oil-ui/` | https://github.com/oil-oil/oil-ui | v0.31.0，`5b03da0997e0c57650880c521bf5462696d3efe9` | MIT |

- **安装方式**：从上游仓库 `main` 分支的 tarball 解压后整目录复制，未改写、未摘要、未裁剪任何上游文件；来源信息（仓库、分支、提交、获取时间、许可证）记录在 Skill 目录的 `SOURCE.json`，上游 MIT 许可证文本随目录保存在 `LICENSE`。
- **Oil UI**：完整安装上游仓库（`SKILL.md`、`references/`、`scripts/`、`assets/`、`tests/`、README 与 LICENSE）。其可选脚本不依赖 npm 包：`scripts/*.mjs` 需要 Node.js 22+ 与本机 Chrome/Chromium/Edge，`scripts/*.py` 仅使用 Python 标准库；上游 `check_update.py` 会访问 `ui.oiloil.org` 检查版本，属可选联网行为。上述脚本不会被项目构建或运行流程调用。
- 该 Skill 不随 `dist/`、`requirements.txt`、`package.json` 分发，也不被打包进发布包。

## Python 依赖

含 `requirements.txt` 等文件中的直接依赖及其全部传递依赖（取自本地 `.venv` 已安装元数据）。

| 包 | 版本 | 许可证 | 直接依赖 |
| --- | --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |  |
| anyio | 4.15.1 | MIT |  |
| bcrypt | 4.0.1 | Apache Software License | 是 |
| bidict | 0.24.1 | MPL-2.0 |  |
| blinker | 1.9.0 | MIT License |  |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |  |
| charset-normalizer | 3.5.2 | MIT |  |
| click | 8.5.0 | BSD-3-Clause |  |
| cloudpickle | 3.1.2 | BSD License |  |
| colorama | 0.4.6 | BSD License |  |
| filelock | 4.2.0 | MIT |  |
| Flask | 2.3.3 | BSD License | 是 |
| Flask-Cors | 4.0.1 | MIT License | 是 |
| Flask-SocketIO | 5.3.6 | MIT License | 是 |
| fsspec | 2026.9.0 | BSD-3-Clause |  |
| h11 | 0.16.0 | MIT License |  |
| hf-xet | 1.7.0 | Apache-2.0 |  |
| httpcore | 1.0.9 | BSD-3-Clause |  |
| httpx | 0.28.1 | BSD License |  |
| huggingface_hub | 1.33.0 | Apache Software License |  |
| idna | 3.20 | BSD-3-Clause |  |
| iniconfig | 2.3.1 | MIT |  |
| itsdangerous | 2.2.0 | BSD License |  |
| Jinja2 | 3.1.6 | BSD License |  |
| joblib | 1.6.0 | BSD-3-Clause |  |
| markdown-it-py | 4.2.0 | MIT License |  |
| MarkupSafe | 3.0.4 | BSD-3-Clause |  |
| mdurl | 0.1.2 | MIT License |  |
| mpmath | 1.3.0 | BSD License |  |
| narwhals | 2.27.1 | MIT |  |
| networkx | 3.7 | BSD-3-Clause |  |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |  |
| olefile | 0.47 | BSD License | 是 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |  |
| pip | 26.2.1 | MIT |  |
| pluggy | 1.6.0 | MIT License |  |
| Pygments | 2.21.0 | BSD-2-Clause |  |
| pymupdf | 1.28.2 | AGPL-3.0 或 Artifex Commercial（双许可） | 是 |
| pytest | 8.4.0 | MIT License | 是 |
| python-engineio | 4.14.0 | MIT |  |
| python-socketio | 5.11.2 | MIT License | 是 |
| PyYAML | 6.0.3 | MIT License |  |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python |  |
| requests | 2.34.2 | Apache Software License | 是 |
| rich | 15.0.0 | MIT License |  |
| safetensors | 0.8.0 | Apache Software License |  |
| scikit-learn | 1.9.1 | BSD-3-Clause |  |
| scipy | 1.18.1 | BSD License |  |
| sentence-transformers | 6.1.0 | Apache-2.0 | 是 |
| setuptools | 84.0.0 | MIT |  |
| shellingham | 1.5.4 | ISC License (ISCL) |  |
| simple-websocket | 1.1.0 | MIT License |  |
| sympy | 1.14.0 | BSD License |  |
| threadpoolctl | 3.7.0 | BSD-3-Clause |  |
| tokenizers | 0.23.3 | Apache Software License |  |
| tomlkit | 0.15.2 | MIT | 是 |
| torch | 2.14.1+cpu | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT |  |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |  |
| transformers | 5.19.0 | Apache 2.0 License |  |
| typer | 0.27.3 | MIT |  |
| typing_extensions | 4.16.0 | PSF-2.0 |  |
| urllib3 | 2.8.0 | MIT |  |
| Werkzeug | 2.3.8 | BSD License | 是 |
| wsproto | 1.3.2 | MIT |  |

## 前端 / 构建依赖（npm）

含 `package.json` 的 `dependencies` 与 `devDependencies`（含 `esbuild` 的平台可选依赖）。

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| @esbuild/aix-ppc64 | 0.28.1 | MIT |
| @esbuild/android-arm | 0.28.1 | MIT |
| @esbuild/android-arm64 | 0.28.1 | MIT |
| @esbuild/android-x64 | 0.28.1 | MIT |
| @esbuild/darwin-arm64 | 0.28.1 | MIT |
| @esbuild/darwin-x64 | 0.28.1 | MIT |
| @esbuild/freebsd-arm64 | 0.28.1 | MIT |
| @esbuild/freebsd-x64 | 0.28.1 | MIT |
| @esbuild/linux-arm | 0.28.1 | MIT |
| @esbuild/linux-arm64 | 0.28.1 | MIT |
| @esbuild/linux-ia32 | 0.28.1 | MIT |
| @esbuild/linux-loong64 | 0.28.1 | MIT |
| @esbuild/linux-mips64el | 0.28.1 | MIT |
| @esbuild/linux-ppc64 | 0.28.1 | MIT |
| @esbuild/linux-riscv64 | 0.28.1 | MIT |
| @esbuild/linux-s390x | 0.28.1 | MIT |
| @esbuild/linux-x64 | 0.28.1 | MIT |
| @esbuild/netbsd-arm64 | 0.28.1 | MIT |
| @esbuild/netbsd-x64 | 0.28.1 | MIT |
| @esbuild/openbsd-arm64 | 0.28.1 | MIT |
| @esbuild/openbsd-x64 | 0.28.1 | MIT |
| @esbuild/openharmony-arm64 | 0.28.1 | MIT |
| @esbuild/sunos-x64 | 0.28.1 | MIT |
| @esbuild/win32-arm64 | 0.28.1 | MIT |
| @esbuild/win32-ia32 | 0.28.1 | MIT |
| @esbuild/win32-x64 | 0.28.1 | MIT |
| @types/react | 19.2.17 | MIT |
| @types/react-dom | 19.2.3 | MIT |
| @types/three | 0.180.0 | MIT |
| cannon-es | 0.20.0 | MIT |
| csstype | 3.2.3 | MIT |
| esbuild | 0.28.1 | MIT |
| react | 19.2.7 | MIT |
| react-dom | 19.2.7 | MIT |
| scheduler | 0.27.0 | MIT |
| three | 0.180.0 | MIT |
| typescript | 5.9.3 | Apache-2.0 |

## 前端内置的第三方代码（vendored）

以下代码直接内置于本仓库源码中（非 npm 依赖），随前端一起构建分发。

| 组件 | 版本 | 许可证 | 来源 |
| --- | --- | --- | --- |
| qrcode-generator（裁剪版，仅保留核心编码器与 SVG 渲染器） | 1.4.4 | MIT | https://github.com/kazuhikoarase/qrcode-generator |

## 前端 CDN 资源

以下资源由浏览器从 jsDelivr 加载，不随构建产物打包；离线部署时对应能力会降级。

| 组件 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| marked | 15.0.12 | MIT | Markdown 渲染 |
| DOMPurify | 3.4.16 | Apache-2.0 OR MPL-2.0 | Markdown/HTML 净化 |

## 前端本地随包资源

以下资源随仓库分发，由 `/assets/vendor/<path>` 路由直出，不依赖 CDN。

| 组件 | 版本 | 许可证 | 位置 |
| --- | --- | --- | --- |
| KaTeX | 0.16.11 | MIT | `data/assets/vendor/katex/`（含 `LICENSE`、`katex.min.css`、`katex.min.js`、`fonts/`） |

## 合规提示

- **PyMuPDF（AGPL-3.0 或 Artifex 商业许可，双许可）**：AGPL-3.0 属强著佐权许可。若以 AGPL
  版本分发本软件，或通过网络向使用者提供其功能，需按 AGPL 要求提供完整对应源码；若不愿受
  AGPL 约束，可改用 Artifex 商业许可。**分发前请务必确认这一项。** 它用于 PDF/DOC 剧本导入。
- **MPL-2.0（bidict、certifi、tqdm）**：文件级著佐权，只需保留对应文件中的许可声明，无需开源
  本项目自有代码。
- 其余依赖以 MIT / BSD / Apache-2.0 为主，均为宽松许可；分发时保留本清单与各依赖自身的版权
  声明即可。

