# cc-switch 参考记录

本阶段按用户要求由 GPT-6 Luna 研究源码，主代理补查原始文件与 models.dev 实际数据，并独立实现 Python 版本。

## 对应源码

- [modelsDevPricing.ts](https://github.com/farion1231/cc-switch/blob/main/src/lib/modelsDevPricing.ts)：价格数据来自 `https://models.dev/api.json`，`flattenModels` 扁平化供应商/模型，`toModelPricing` 转成定价记录。`cost.input/output/cache_read/cache_write` 直接对应每百万 token 价格，不需再乘一百万。
- [modelsDevAutoSync.ts](https://github.com/farion1231/cc-switch/blob/main/src/lib/modelsDevAutoSync.ts)：`syncModelsDevPricing` 与 6 小时启动同步节流，保存同步结果，按所选项目导入。
- [model_fetch.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/services/model_fetch.rs)：`fetch_models`、`build_models_url_candidates` 处理模型列表 URL 候选、格式差异与鉴权。
- [model_fetch commands](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/commands/model_fetch.rs)：前端命令入口、覆盖模型列表地址及接口格式参数。
- [OpenAI 模型列表](https://developers.openai.com/api/reference/python/resources/models/methods/list)：`GET /models` 返回可见模型 ID、所有者等基本信息，不等于模型推理能力说明。

## 在 bot 中采用的设计

采用公共价格目录、定时缓存、失败回退、模型列表发现和连接归一化。Python 实现位于 `model_pricing.py`、`model_services.py`，由配置管理器、CLI 和 Web 复用。

bot 保留人民币统计，显式配置 USD→CNY 汇率，手工定价始终优先，避免覆盖现有定价和预算。供应商/模型按精确 ID 匹配，公共分档价格暂要求手工配置，避免把不同网关、模型后缀或档位当作同一收费标准。

模型接入只覆盖现有 Chat Completions 兼容协议。没有搬入 cc-switch 的桌面系统代理、OAuth、Anthropic/Google 原生协议转换或自定义请求头体系。支持同源模型列表覆盖，禁止带密钥跟随重定向。

## 来源说明

[cc-switch LICENSE](https://github.com/farion1231/cc-switch/blob/main/LICENSE) 是 MIT。本次借鉴设计并独立编写 Python 实现，没有复制其 TypeScript/Rust 源码，也没有将整个价格目录打包进仓库。下载缓存仅用于运行时估价；实际账单以服务商为准。
