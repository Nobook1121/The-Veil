# 第三方依赖许可证清单

本文件由 `scripts` 侧的许可收集流程生成，列出「帷幕 The Veil」运行与构建所使用第三方依赖的
名称、版本与许可证标识，供分发时合规参考。许可证标识取自各发行包自身的元数据；
如需完整许可证文本，请参见对应上游包（Python 包可在 `pip show <包名>`，
Node 包可在 `node_modules/<包名>/LICENSE` 查看）。

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

