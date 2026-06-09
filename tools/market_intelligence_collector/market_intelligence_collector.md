# MIC：公司/行业信息采集与结构化分析工具设计方案 V0.3

> 文档用途：供讨论、评审、拆分功能需求和后续工程实现参考。  
> 设计重点：搜索结果驱动、模型完全配置化、减少不必要模型调用、结构化输出贴近分析师需求、关系型数据入库但不做复杂图数据库。

---

## 目录

1. [工具定位](#1-工具定位)
2. [总体设计原则](#2-总体设计原则)
3. [外部接口基础](#3-外部接口基础)
4. [总体架构](#4-总体架构)
5. [目标画像 Target Profile](#5-目标画像-target-profile)
6. [分析师信息需求分类](#6-分析师信息需求分类)
7. [典型任务](#7-典型任务)
8. [Query Planner 搜索计划生成层](#8-query-planner-搜索计划生成层)
9. [搜索结果初筛 Search Hit Triage](#9-搜索结果初筛-search-hit-triage)
10. [Link Reader 与临时内容处理](#10-link-reader-与临时内容处理)
11. [模型调用计划器 Model Call Planner](#11-模型调用计划器-model-call-planner)
12. [模型输出 Schema：Bundle Extraction](#12-模型输出-schemabundle-extraction)
13. [结构化对象设计](#13-结构化对象设计)
14. [多模型结果合并](#14-多模型结果合并)
15. [调用治理：减少无效模型调用](#15-调用治理减少无效模型调用)
16. [存储设计](#16-存储设计)
17. [本地校验](#17-本地校验)
18. [运行流程](#18-运行流程)
19. [Batch Report](#19-batch-report)
20. [Analyst Agent API](#20-analyst-agent-api)
21. [配置文件设计](#21-配置文件设计)
22. [反馈与优化](#22-反馈与优化)
23. [最终运行示例](#23-最终运行示例)
24. [核心变化总结](#24-核心变化总结)
25. [参考资料](#25-参考资料)

---

# 1. 工具定位

MIC 是一个面向分析师和分析员 Agent 的信息发现、读取、结构化分析工具。

它的核心任务是：

> 给定公司、行业、产品、产业链节点或专题，通过搜索引擎生成和执行搜索计划，读取搜索结果对应的链接内容，调用配置的一个或多个模型进行结构化分析，把分析师需要的事实、指标、事件、关系型数据、风险、催化剂和后续问题保存到本地数据库，供后续分析员 Agent 二次判断行情影响。

系统关注的不是“保存网页”，而是：

```text
发现信息
  → 判断信息是否有分析价值
  → 抽取分析师需要的结构化内容
  → 合并多个模型的判断
  → 保存链接和结构化结果
  → 供后续 Agent 检索、汇总、复盘
```

---

# 2. 总体设计原则

## 2.1 模型由配置决定

系统不预设“便宜模型 / 强模型 / 默认单模型 / 默认多模型”。

模型层只提供机制：

```text
1. 可配置模型供应商
2. 可配置模型优先级
3. 可配置串行 fallback
4. 可配置并行多模型
5. 可配置按任务选择模型
6. 可配置结果合并方式
7. 可配置调用预算和触发条件
8. 可记录每次模型调用成本、token、延迟和输出质量
```

也就是说：

```text
用哪个模型，由配置决定。
是否多个模型一起分析，由配置决定。
多个模型怎么合并，由策略配置。
系统负责减少无效调用，而不是替业务方决定模型档位。
```

## 2.2 只持久化链接和结构化结果

系统读取网页内容用于临时分析，但持久化时主要保存：

```text
搜索结果链接
来源元数据
读取记录
内容指纹
结构化事实
指标观察
事件卡片
关系型记录
风险信号
催化剂
分析师摘要
后续问题
模型判断记录
合并结果
```

正文、HTML、PDF、截图不作为默认持久化对象。

## 2.3 关系型数据要提取，但不做复杂图数据库

分析师需要“谁是谁的客户、供应商、竞争对手、合作方、监管方、项目方”等关系型信息。

这些信息要提取，但实现上用关系型表即可，例如：

```text
entity_relation 表
customer_supplier_signal 表
contract_counterparty 表
project_participant 表
policy_subject 表
```

这样可以满足分析师查询，不需要上图数据库，也不需要做复杂知识图谱。

## 2.4 搜索结果驱动阅读

系统通过搜索引擎或搜索 API 发现链接，然后读取这些搜索结果链接。

工作方式：

```text
搜索 query
  → search hits
  → URL 去重
  → 选择要读取的链接
  → 读取该链接内容
  → 临时正文分析
  → 保存结构化结果
```

网页内出现的新链接可以被记录为“后续待搜索线索”，但不作为本轮自动深入爬取对象。

---

# 3. 外部接口基础

模型接入层建议以 **OpenAI-compatible Adapter** 为主，因为 DeepSeek、硅基流动、Qwen / 阿里云百炼、OpenClaw Gateway 都可以纳入统一适配框架。

## 3.1 DeepSeek

DeepSeek 官方文档说明其 API 使用 OpenAI / Anthropic 兼容格式，OpenAI-compatible `base_url` 为：

```text
https://api.deepseek.com
```

并支持通过 OpenAI SDK 调用。DeepSeek 还提供 JSON Output 功能，可通过：

```json
{"response_format": {"type": "json_object"}}
```

要求模型输出合法 JSON。但仍需要在 prompt 中给出 JSON 输出要求，并合理设置 `max_tokens` 防止截断。

## 3.2 硅基流动 SiliconFlow

硅基流动的 Chat Completions 接口为 OpenAI 风格接口，支持 `response_format`、`tools`，响应中包含 `usage` 等字段，适合纳入统一模型调用和成本记录。

典型 endpoint：

```text
https://api.siliconflow.cn/v1
```

## 3.3 Qwen / 阿里云百炼

阿里云 Model Studio 文档说明，Qwen 模型支持 OpenAI-compatible Chat 接口，只需调整 API Key、BASE_URL 和模型名。

国内常用 compatible endpoint：

```text
https://dashscope.aliyuncs.com/compatible-mode/v1
```

## 3.4 OpenClaw Gateway

OpenClaw Gateway 可以开启 OpenAI-compatible Chat Completions endpoint：

```text
POST /v1/chat/completions
```

在启用 HTTP surface 后，还可以提供：

```text
/v1/models
/v1/embeddings
/v1/responses
```

这些请求会作为正常 Gateway agent run 执行，继承 Gateway 的路由、权限和配置。

---

# 4. 总体架构

```text
MIC Core

1. Target Profile Manager
   目标画像管理

2. Analyst Intelligence Taxonomy
   分析师信息需求分类

3. Query Planner
   搜索计划生成

4. Search Provider Layer
   搜索引擎与搜索 API 适配

5. Search Hit Triage
   搜索结果初筛与去重

6. Link Reader
   搜索结果链接读取

7. Content Preprocessor
   临时正文清洗、段落选择、表格提取

8. Model Call Planner
   模型调用计划器

9. Model Adapter Layer
   DeepSeek / Qwen / 硅基流动 / OpenClaw / 本地模型 / 其他接口

10. Structured Intelligence Extractor
    结构化信息抽取

11. Multi-model Merger
    多模型结果合并

12. Intelligence Store
    结构化数据库

13. Analyst Agent API
    给分析员 Agent 调用

14. Feedback & Evaluation
    反馈、复盘、规则优化
```

---

# 5. 目标画像 Target Profile

Target Profile 是扁平化配置，不做复杂知识图谱。

## 5.1 公司目标画像

```yaml
target_profile:
  target_id: "company_300750"
  type: company
  canonical_name: "宁德时代新能源科技股份有限公司"
  aliases:
    - 宁德时代
    - CATL
    - Contemporary Amperex Technology
    - 300750
  markets:
    - A股
  products:
    - 动力电池
    - 储能电池
    - 锂电池
  business_segments:
    - 动力电池系统
    - 储能系统
    - 电池材料
  regions:
    - 中国
    - 欧洲
    - 北美
  known_customers:
    - 特斯拉
    - 理想汽车
    - 蔚来
    - 宝马
  known_suppliers:
    - 碳酸锂供应商
    - 正极材料供应商
    - 负极材料供应商
  upstream_terms:
    - 碳酸锂
    - 氢氧化锂
    - 正极材料
    - 负极材料
    - 电解液
  downstream_terms:
    - 新能源汽车
    - 储能
    - 电网侧储能
  competitors:
    - 比亚迪
    - 中创新航
    - 国轩高科
```

## 5.2 行业目标画像

```yaml
target_profile:
  target_id: "industry_pv_glass"
  type: industry
  canonical_name: "光伏玻璃"
  aliases:
    - 光伏压延玻璃
    - solar glass
  upstream_terms:
    - 纯碱
    - 石英砂
    - 天然气
  downstream_terms:
    - 光伏组件
    - 光伏装机
    - 分布式光伏
  core_metrics:
    - 价格
    - 库存
    - 开工率
    - 产能利用率
    - 新增产能
    - 出口
  representative_companies:
    - 福莱特
    - 信义光能
    - 亚玛顿
```

---

# 6. 分析师信息需求分类

这部分是整个工具的“分析师需求字典”。Query Planner、模型抽取 Schema、存储表都围绕它设计。

## 6.1 经营动态

```text
订单
合同
中标
招标
在手订单
出货量
销量
交付量
产量
产能
投产
扩产
停产
复产
开工率
产能利用率
库存
交付周期
渠道库存
```

## 6.2 财务领先指标

```text
收入变化线索
毛利率变化线索
单位成本变化
费用变化
现金流变化
应收账款
存货
合同负债
预收款
减值
坏账
资本开支
研发投入
盈利预告
业绩快报
业绩修正
```

## 6.3 价格、成本与利润率

```text
产品价格
原材料价格
涨价
降价
折扣
返利
运费
能源成本
汇率
关税
单吨利润
单位毛利
价差
成本传导
价格战
```

## 6.4 客户变化

```text
新增大客户
客户流失
客户认证
客户份额变化
客户削单
客户提货节奏
客户库存
客户自身销量
客户新品发布
客户资本开支
客户供应商名单
```

## 6.5 供应商与上游

```text
供应商新增
供应商退出
供应商涨价
供应商停产
供应商检修
供应商事故
供应中断
原材料供需
进口依赖
供应合同
长协价格
```

## 6.6 行业供需

```text
行业价格
行业库存
行业开工率
行业产能利用率
新增产能
淘汰产能
需求变化
出口
进口
海关数据
旺季淡季
补库去库
供需缺口
```

## 6.7 政策与监管

```text
产业政策
补贴政策
准入门槛
行业标准
环保政策
安全政策
能耗政策
碳排政策
出口管制
反倾销
关税
制裁
地方招商政策
项目备案
审批许可
监管处罚
```

## 6.8 竞争格局

```text
竞争对手价格动作
竞争对手产能动作
竞争对手客户突破
竞争对手中标
市场份额变化
价格战
渠道竞争
技术路线竞争
替代品竞争
```

## 6.9 技术与产品

```text
新产品
产品认证
客户验证
良率
技术路线
专利
工艺升级
降本技术
替代技术
产品召回
质量问题
```

## 6.10 项目与产能建设

```text
新建项目
扩建项目
并购项目
项目备案
环评
能评
开工
试生产
投产
达产
延期
取消
投资金额
设计产能
```

## 6.11 风险事件

```text
安全事故
环保处罚
质量召回
诉讼仲裁
监管处罚
财务异常
债务违约
管理层变动
裁员
停工
供应中断
客户违约
舆情风险
地缘风险
```

## 6.12 资本市场与公司治理

```text
定增
回购
减持
增持
股权激励
员工持股
分红
并购重组
资产处置
债券发行
评级变化
审计意见
会计政策变更
实际控制人变化
董事高管变动
```

## 6.13 海外与贸易

```text
海外订单
海外产能
海外客户
出口数据
进口数据
关税
反倾销
制裁
本地化生产
海外认证
海外监管
航运变化
港口拥堵
汇率影响
```

## 6.14 替代数据线索

```text
招聘变化
工厂招聘
经销商反馈
消费者评价
投诉
App 下载量
网站流量报道
社交媒体热度
新闻情绪
门店客流报道
航运库存报道
卫星库存报道
行业协会高频数据
```

## 6.15 催化剂

```text
财报日期
业绩说明会
投资者日
新品发布
政策会议
招标结果日期
项目投产日期
审批截止日
法院日期
展会
行业会议
锁定期解禁
分红除权
```

## 6.16 分析师后续问题

```text
金额是否确认
客户是否确认
是否有官方公告
是否有多个来源交叉验证
是否影响收入确认
是否影响毛利率
是否影响供需
是否影响估值假设
是否需要继续搜索
是否需要人工确认
```

---

# 7. 典型任务

## 7.1 公司日常监控

```text
最近 7 / 30 / 90 天公司经营动态
订单、合同、中标、产能、销量、价格变化
重大风险、政策影响、客户变化
提取高优先级事件和后续问题
```

## 7.2 公司财报前线索收集

```text
收入端：订单、出货、客户、价格
利润端：原材料、单位成本、产品价格、毛利率
现金流端：回款、应收、存货、合同负债
预期差：业绩预告、调研纪要、渠道反馈、竞品表现
```

## 7.3 行业供需监控

```text
价格
库存
开工率
产能利用率
新增产能
检修停产
出口进口
需求端高频数据
行业政策和标准变化
```

## 7.4 客户供应链监控

```text
目标公司的大客户变化
目标公司的供应商变化
客户自身经营变化对目标公司的传导
上游原材料变化对目标公司成本的传导
供应中断、长协、涨价、削单、认证进展
```

## 7.5 政策专题监控

```text
某行业政策变化
政策影响的公司列表
政策涉及的产品、地区、产能、审批、补贴、处罚
政策对收入、成本、资本开支、估值风险的影响通道
```

## 7.6 风险专题监控

```text
事故
处罚
诉讼
召回
债务
管理层变动
环保安全
负面舆情
供应链中断
```

## 7.7 催化剂日历生成

```text
财报日期
业绩说明会
招标结果
项目投产
新品发布
政策会议
审批截止日
法院日期
股东大会
解禁日期
```

## 7.8 关系型信息抽取

```text
谁是客户
谁是供应商
谁是竞争对手
谁是合作方
谁参与某项目
谁中标
谁招标
谁被处罚
谁和谁签订合同
谁向谁供货
谁从谁采购
```

## 7.9 跨来源验证

```text
同一事件是否有多个来源
哪个来源最原始
是否有官方确认
不同来源的金额、日期、客户、产能是否冲突
是否需要继续搜索验证
```

---

# 8. Query Planner 搜索计划生成层

Query Planner 是最关键模块之一。

它的目标不是生成最多 query，而是生成**更贴近分析师需求、能覆盖关键变化、能控制模型调用量**的 query。

## 8.1 输入

```yaml
query_plan_input:
  target_profile:
    target_name: "某公司"
    target_type: company
    aliases:
      - 简称
      - 股票代码
      - 英文名
    products:
      - 产品A
      - 产品B
    customers:
      - 客户A
      - 客户B
    suppliers:
      - 供应商A
    competitors:
      - 竞争对手A
    upstream_terms:
      - 原材料A
    downstream_terms:
      - 下游行业A

  task_profile:
    focus:
      - operating_update
      - customer_change
      - supply_chain
      - policy
      - financial_leading_indicator
      - risk
    time_window: "30d"
    regions:
      - 中国
      - 海外
    languages:
      - zh
      - en

  budget_profile:
    max_queries: 80
    max_search_hits: 800
    max_links_to_read: 100
    max_model_calls: 30
```

## 8.2 Query Family

每个搜索词属于一个 query family。系统根据任务选择 family。

### A. 官方公告与投资者关系

```text
{company} 公告
{company} 重大合同 公告
{company} 投资者关系
{company} 调研纪要
{company} 业绩说明会
{company} 投资者问答
{company} 年报
{company} 季报
{company} 半年报
{company} 经营情况
{company} 业绩预告
{company} 业绩快报
```

### B. 订单、合同、招投标

```text
{company} 订单
{company} 合同
{company} 中标
{company} 招标
{company} 供货协议
{company} 框架协议
{company} 长协
{company} 在手订单
{company} {customer} 订单
{company} {product} 中标
{industry} 招标
{industry} 中标
```

### C. 经营指标

```text
{company} 销量
{company} 出货量
{company} 交付量
{company} 产量
{company} 库存
{company} 开工率
{company} 产能利用率
{company} 投产
{company} 扩产
{company} 停产
{company} 复产
{company} 检修
```

### D. 价格、成本、毛利率

```text
{product} 价格
{product} 涨价
{product} 降价
{company} 调价
{industry} 价格
{industry} 毛利率
{upstream_material} 价格
{upstream_material} 库存
{industry} 成本
{industry} 价差
{company} 原材料 成本
```

### E. 客户与供应商

```text
{company} 大客户
{company} 客户
{company} 供应商
{company} 新客户
{company} 客户认证
{company} {customer} 供货
{customer} 供应商 {product}
{supplier} 停产 {company}
{supplier} 涨价 {company}
{customer} 削单 {company}
```

### F. 行业供需

```text
{industry} 供需
{industry} 库存
{industry} 开工率
{industry} 产能利用率
{industry} 新增产能
{industry} 淘汰产能
{industry} 检修
{industry} 停产
{industry} 需求
{industry} 出口
{industry} 进口
{industry} 海关数据
```

### G. 政策与监管

```text
{industry} 政策
{industry} 监管
{industry} 补贴
{industry} 标准
{industry} 准入
{industry} 能耗
{industry} 环保
{industry} 安全
{industry} 出口管制
{industry} 反倾销
{industry} 关税
{company} 处罚
{company} 监管
```

### H. 竞争与技术

```text
{company} 竞争对手
{competitor} {product} 价格
{competitor} 扩产
{competitor} 中标
{industry} 价格战
{industry} 市场份额
{product} 技术路线
{product} 替代技术
{company} 新产品
{company} 专利
```

### I. 风险

```text
{company} 事故
{company} 安全事故
{company} 环保处罚
{company} 质量问题
{company} 召回
{company} 诉讼
{company} 仲裁
{company} 债务
{company} 裁员
{company} 停工
{company} 负面舆情
```

### J. 财务领先指标

```text
{company} 毛利率
{company} 合同负债
{company} 应收账款
{company} 存货
{company} 现金流
{company} 减值
{company} 业绩修正
{company} 盈利预警
{company} 资本开支
{company} 研发投入
```

### K. 催化剂

```text
{company} 财报 日期
{company} 业绩说明会
{company} 投资者日
{company} 新品发布
{industry} 会议
{industry} 政策会议
{project} 投产 时间
{tender} 中标结果
```

### L. 替代数据线索

```text
{company} 招聘
{company} 工厂招聘
{company} 经销商 反馈
{product} 投诉
{product} 评价
{company} app 下载量
{company} 网站流量
{industry} 航运 库存
{industry} 港口 库存
{industry} 卫星 库存
```

## 8.3 Query Scoring

每个 query 在执行前先评分。

```text
query_score =
  主题优先级
  + 来源可信度预期
  + 具体事实预期
  + 时间敏感度
  + 覆盖缺口价值
  + 与目标实体匹配度
  - 与已执行 query 的重复度
```

示例：

```json
{
  "query": "某公司 某客户 供货协议",
  "family": "customer_supplier",
  "score": 87,
  "why": [
    "包含目标公司",
    "包含已知大客户",
    "可能产生订单或客户关系信息",
    "与收入预测相关"
  ]
}
```

低分 query 不进入搜索计划，高分 query 优先执行。

## 8.4 Source Pack

Source Pack 不是固定爬网站，而是帮助搜索引擎优先找到高质量来源。

```yaml
source_packs:
  china_exchange:
    - "site:cninfo.com.cn {company}"
    - "site:sse.com.cn {company}"
    - "site:szse.cn {company}"

  hk_exchange:
    - "site:hkexnews.hk {company}"

  us_filing:
    - "site:sec.gov {company}"
    - "{company} 10-K"
    - "{company} 10-Q"
    - "{company} 8-K"

  policy:
    - "site:gov.cn {industry} 政策"
    - "site:miit.gov.cn {industry}"
    - "site:ndrc.gov.cn {industry}"
    - "site:customs.gov.cn {industry} 出口"

  tender:
    - "{company} 中标"
    - "{company} 招标"
    - "{industry} 中标"
    - "{industry} 招标"
```

---

# 9. 搜索结果初筛 Search Hit Triage

Search Hit Triage 的目的是减少后续读取和模型调用。

这一层优先用规则和已有元数据，不依赖模型。

## 9.1 初筛输入

```json
{
  "query": "...",
  "title": "...",
  "snippet": "...",
  "url": "...",
  "domain": "...",
  "rank": 3,
  "provider": "baidu",
  "publish_time_guess": "..."
}
```

## 9.2 初筛特征

```text
标题是否包含目标公司 / 行业 / 产品 / 客户
摘要是否包含金额、数量、日期、比例、客户、供应商、政策、处罚等强事实词
来源类型
排名
发布时间
URL 是否重复
是否已分析过
是否与已有事件重复
是否属于高价值 query family
是否属于覆盖缺口
```

## 9.3 初筛输出

```json
{
  "source_link_id": "link_xxx",
  "triage_decision": "read | link_record_only | skip_for_now",
  "read_priority": 82,
  "matched_signals": [
    "target_entity_match",
    "amount_mentioned",
    "customer_keyword",
    "order_keyword"
  ],
  "model_call_hint": {
    "need_model": true,
    "suggested_task": "bundle_extraction",
    "reason": "摘要中出现目标公司、大客户和金额"
  }
}
```

---

# 10. Link Reader 与临时内容处理

## 10.1 链接读取

Link Reader 只负责读取搜索结果 URL，并生成临时分析文本。

```text
读取 URL
  → 获取标题、发布时间、来源
  → 提取正文文本、表格、列表
  → 临时清洗
  → 选取相关段落
  → 交给模型或本地抽取器
  → 丢弃原始正文缓存
```

## 10.2 段落选择 Passage Selection

这是减少模型调用成本和 token 的关键。

系统不把全文直接发送给模型，而是先选段落。

选取规则：

```text
标题
摘要
首段
发布时间附近信息
包含目标实体的段落
包含客户 / 供应商 / 产品 / 政策词的段落
包含金额、数量、百分比、日期的段落
包含表格的行列
包含风险词的段落
包含结论或摘要的段落
```

输出给模型的内容是：

```json
{
  "source_metadata": {
    "title": "...",
    "url": "...",
    "source": "...",
    "publish_time": "..."
  },
  "selected_passages": [
    {
      "passage_id": "p1",
      "section": "正文第3段",
      "text": "..."
    },
    {
      "passage_id": "t1",
      "section": "表格1",
      "text": "..."
    }
  ]
}
```

## 10.3 一次模型调用尽量完成多类抽取

为减少调用次数，同一篇链接内容应尽量用一个 `bundle_extraction` 任务完成：

```text
来源质量判断
分析师摘要
事实抽取
指标抽取
事件抽取
关系型记录抽取
风险抽取
催化剂抽取
后续问题生成
```

只有当配置要求拆分，或内容太长，才拆成多个模型任务。

---

# 11. 模型调用计划器 Model Call Planner

它不决定“用便宜还是强”，它只决定：

```text
是否需要调用模型
调用哪个配置任务
是否使用一个模型
是否使用多个模型
是否串行 fallback
是否并行 ensemble
是否复用已有结果
是否需要继续调用
```

## 11.1 模型调用模式

```yaml
call_modes:
  no_model:
    description: "规则可完成，或链接仅记录"

  single_model:
    description: "调用配置中的一个模型"

  priority_fallback:
    description: "按优先级调用，失败或输出不合格时调用下一个"

  parallel_ensemble:
    description: "多个模型并行分析，然后合并"

  cascade:
    description: "先调用一个模型，满足触发条件再调用后续模型"

  arbitration:
    description: "结果冲突时调用配置的仲裁模型或仲裁模型组"

  batch_triage:
    description: "多个搜索结果摘要合并到一次模型调用中做初筛"
```

## 11.2 模型配置示例

```yaml
model_registry:
  deepseek_v4_pro:
    provider_type: openai_compatible_direct
    provider: deepseek
    endpoint: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
    model: "deepseek-v4-pro"
    enabled: true
    capabilities:
      json_output: true
      tool_calling: true
      reasoning: true
    tags:
      - cn
      - reasoning
      - structured

  qwen_plus:
    provider_type: openai_compatible_direct
    provider: qwen_dashscope
    endpoint: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env: "DASHSCOPE_API_KEY"
    model: "qwen-plus"
    enabled: true
    capabilities:
      json_output: true
      tool_calling: true
      long_context: model_dependent
    tags:
      - cn
      - structured

  siliconflow_qwen:
    provider_type: openai_compatible_direct
    provider: siliconflow
    endpoint: "https://api.siliconflow.cn/v1"
    api_key_env: "SILICONFLOW_API_KEY"
    model: "Qwen/Qwen3-32B"
    enabled: true
    capabilities:
      json_output: true
      tool_calling: model_dependent
    tags:
      - cn
      - platform

  openclaw_research:
    provider_type: openai_compatible_gateway
    provider: openclaw
    endpoint: "http://127.0.0.1:18789/v1"
    api_key_env: "OPENCLAW_GATEWAY_TOKEN"
    model: "openclaw/research"
    enabled: false
    capabilities:
      chat: true
      gateway_routing: true
      agent_routing: true
    tags:
      - gateway
      - configurable_backend
```

注意这里不写“便宜 / 强”的系统默认分类。模型标签、优先级、权重、任务用途都由配置决定。

## 11.3 任务级模型策略

```yaml
model_policies:
  serp_batch_triage:
    call_mode: batch_triage
    models:
      - model_id: qwen_plus
        priority: 1
        weight: 1.0
      - model_id: deepseek_v4_pro
        priority: 2
        weight: 1.0
    batch_size: 20
    trigger:
      use_model_when_rule_score_between: [45, 75]

  bundle_extraction:
    call_mode: priority_fallback
    models:
      - model_id: qwen_plus
        priority: 1
        weight: 0.5
      - model_id: deepseek_v4_pro
        priority: 2
        weight: 0.5
    fallback_when:
      - request_failed
      - json_invalid
      - schema_invalid
      - confidence_below_threshold

  high_value_parallel_analysis:
    call_mode: parallel_ensemble
    models:
      - model_id: qwen_plus
        weight: 0.4
      - model_id: deepseek_v4_pro
        weight: 0.4
      - model_id: siliconflow_qwen
        weight: 0.2
    trigger:
      - source_type in ["official", "exchange", "regulator"]
      - materiality_score >= 80

  arbitration:
    call_mode: arbitration
    models:
      - model_id: deepseek_v4_pro
        priority: 1
      - model_id: openclaw_research
        priority: 2
    trigger:
      - model_conflict == true
      - field_conflict in ["amount", "customer", "event_date", "impact_direction"]
```

## 11.4 减少不必要模型调用的机制

这里是费用控制的核心，但不绑定具体模型。

### A. URL 与内容指纹复用

```text
同一 canonical_url 已分析过，复用结果
同一 content_hash 已分析过，复用结果
标题和摘要高度相似，合并到同一候选组
同一事件已有高置信结构化记录，只做补充抽取
```

### B. Query 结果去重

```text
多个搜索引擎返回同一链接，只读一次
多个 query 返回同一链接，只读一次
同一新闻转载源只选择更原始或排名更高的来源
```

### C. 规则先行

```text
标题、摘要、来源、时间足够判断低价值时，不调用模型
标题、摘要已包含明确事实时，可以先进入读取队列，不做模型初筛
明显重复内容进入 link_record_only
```

### D. 批量初筛

把多个搜索结果摘要放到一次模型调用里判断：

```json
{
  "task": "serp_batch_triage",
  "items": [
    {"id": "hit_1", "title": "...", "snippet": "..."},
    {"id": "hit_2", "title": "...", "snippet": "..."}
  ]
}
```

这样避免“每条搜索结果一次模型调用”。

### E. 一次性 Bundle Extraction

一篇文章尽量一次调用完成多类抽取：

```text
brief
facts
metrics
events
relations
risks
catalysts
questions
```

避免同一篇文章分别调用“摘要模型、事件模型、风险模型、关系模型”。

### F. 段落选择

只把相关段落给模型，而不是全文。

### G. 早停机制

```text
第一个模型输出已经 schema_valid 且 confidence 足够
  → 不继续 fallback

并行模型结果高度一致
  → 不调用仲裁

同一事件已有官方来源确认
  → 后续低质量来源只做 link_record
```

### H. Provider 缓存与前缀复用

部分模型服务支持 prompt/cache 相关能力时，可以把稳定的 system prompt、schema、rubric 固定在前缀里，提高缓存命中。DeepSeek 文档说明其 API 默认开启上下文硬盘缓存，并在 usage 字段中返回 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`，这类字段可以用于统计缓存效果。

---

# 12. 模型输出 Schema：Bundle Extraction

系统建议把每篇链接内容的模型分析统一成一个 Bundle。

## 12.1 输入

```json
{
  "task": "bundle_extraction",
  "target_profile": {
    "target_name": "某公司",
    "type": "company",
    "aliases": ["..."],
    "products": ["..."],
    "customers": ["..."],
    "suppliers": ["..."]
  },
  "source_metadata": {
    "source_link_id": "link_xxx",
    "title": "...",
    "url": "...",
    "source_name": "...",
    "publish_time": "..."
  },
  "selected_passages": [
    {
      "passage_id": "p1",
      "section": "正文第3段",
      "text": "..."
    }
  ],
  "required_output": [
    "brief",
    "facts",
    "metrics",
    "events",
    "relations",
    "risks",
    "catalysts",
    "analyst_questions"
  ]
}
```

## 12.2 输出

```json
{
  "schema_version": "bundle_extraction_v0.3",
  "source_link_id": "link_xxx",
  "decision": "save_structured | link_only | skip",
  "overall_score": 0,
  "confidence": 0.0,

  "source_quality": {
    "source_type": "official | exchange | regulator | company | media | industry | forum | social | unknown",
    "is_original_source": true,
    "source_credibility_score": 0.0,
    "risk_flags": []
  },

  "brief": {
    "one_sentence": "...",
    "what_happened": "...",
    "why_it_matters": "...",
    "affected_business_lines": [],
    "impact_channels": [],
    "time_horizon": "intraday | 1w | 1m | quarter | annual | long_term | unclear",
    "uncertainty": "..."
  },

  "facts": [],
  "metrics": [],
  "events": [],
  "relations": [],
  "risks": [],
  "catalysts": [],
  "analyst_questions": [],
  "coverage_gaps": []
}
```

---

# 13. 结构化对象设计

## 13.1 SourceLink

```json
{
  "source_link_id": "link_xxx",
  "url": "...",
  "canonical_url": "...",
  "title": "...",
  "domain": "...",
  "source_name": "...",
  "source_type": "official | exchange | regulator | company | media | industry | forum | social | unknown",
  "document_type": "html | pdf | filing | announcement | forum_post | social_post | data_page",
  "publish_time_guess": "...",
  "retrieved_at": "...",
  "search_provider": "...",
  "query": "...",
  "rank": 3,
  "snippet": "...",
  "access_profile_id": "...",
  "content_hash": "...",
  "read_status": "read | link_record_only | failed"
}
```

## 13.2 AnalysisBrief

```json
{
  "brief_id": "brief_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "one_sentence": "...",
  "what_happened": "...",
  "why_it_matters": "...",
  "affected_business_lines": ["..."],
  "impact_channels": ["revenue", "margin", "cost", "supply", "demand", "valuation", "risk"],
  "time_horizon": "1w | 1m | quarter | annual | long_term",
  "confidence": 0.0
}
```

## 13.3 FactItem

```json
{
  "fact_id": "fact_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "fact_type": "order | sales | production | inventory | capacity | price | cost | policy | customer | supplier | risk | finance | product | technology",
  "fact_statement": "...",
  "entities": {
    "subject": "...",
    "object": "...",
    "product": "...",
    "region": "..."
  },
  "metrics": {
    "amount": null,
    "currency": null,
    "volume": null,
    "unit": null,
    "yoy": null,
    "mom": null
  },
  "period": "...",
  "direction": "positive | negative | neutral | mixed | unclear",
  "evidence_locator": {
    "passage_id": "p1",
    "section": "正文第3段",
    "table_id": null
  },
  "confidence": 0.0
}
```

## 13.4 MetricObservation

```json
{
  "metric_id": "metric_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "metric_name": "开工率",
  "metric_value": 72.5,
  "unit": "%",
  "period": "2026-06",
  "scope": {
    "product": "光伏玻璃",
    "region": "中国",
    "segment": null
  },
  "comparison": {
    "yoy": null,
    "mom": 3.2,
    "wow": null
  },
  "interpretation": "...",
  "impact_channels": ["supply", "margin"],
  "confidence": 0.0
}
```

## 13.5 EventCard

```json
{
  "event_id": "evt_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "event_type": "major_order | tender | price_change | capacity_change | policy_change | customer_change | supplier_change | risk_event | earnings_change | financing | mna | product_launch | management_change",
  "event_date": "...",
  "summary": "...",
  "entities": {
    "subject": "...",
    "counterparty": "...",
    "regulator": null,
    "product": "..."
  },
  "metrics": {
    "amount": null,
    "currency": null,
    "capacity": null,
    "volume": null
  },
  "impact": {
    "direction": "positive | negative | mixed | unclear",
    "channels": ["revenue", "margin", "cost", "supply", "demand", "valuation", "risk"],
    "horizon": "intraday | 1w | 1m | quarter | annual | long_term",
    "magnitude_guess": "low | medium | high | unknown"
  },
  "source_corroboration_status": "single_source | multi_source | official_confirmed | conflicting",
  "confidence": 0.0
}
```

## 13.6 RelationRecord

关系型数据用关系表保存，不用图数据库。

```json
{
  "relation_id": "rel_xxx",
  "source_link_id": "link_xxx",
  "subject_entity": {
    "name": "某公司",
    "type": "company",
    "ticker": "..."
  },
  "relation_type": "customer_of | supplier_of | competitor_of | partner_of | subsidiary_of | parent_of | regulator_of | contractor_of | investor_of | project_owner_of | distributor_of",
  "object_entity": {
    "name": "某客户",
    "type": "company"
  },
  "qualifiers": {
    "product": "某产品",
    "region": "中国",
    "period": "2026Q2",
    "amount": null,
    "share": null,
    "status": "new | existing | lost | rumored | confirmed"
  },
  "evidence_locator": {
    "passage_id": "p2",
    "section": "正文第5段"
  },
  "confidence": 0.0
}
```

### 关系类型建议

```text
customer_of
supplier_of
competitor_of
partner_of
distributor_of
contractor_of
project_owner_of
project_participant_of
regulator_of
investor_of
subsidiary_of
parent_of
product_of
facility_of
brand_of
```

这类关系可以服务很多分析师问题：

```text
过去 90 天新增了哪些客户关系？
某公司和某客户之间是否出现订单变化？
某原材料供应商停产影响哪些目标公司？
某政策涉及哪些公司和产品？
```

## 13.7 CustomerSupplierSignal

```json
{
  "signal_id": "cs_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "signal_type": "new_customer | customer_loss | customer_order | customer_cut | supplier_price_increase | supplier_disruption | certification | share_change",
  "customer_or_supplier": "...",
  "product": "...",
  "business_meaning": "...",
  "impact_channels": ["revenue", "cost", "supply"],
  "confidence": 0.0
}
```

## 13.8 PriceCostMarginSignal

```json
{
  "signal_id": "pcm_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "signal_type": "product_price_up | product_price_down | raw_material_cost_up | raw_material_cost_down | spread_change | margin_pressure | margin_recovery",
  "product_or_material": "...",
  "value": null,
  "unit": null,
  "period": "...",
  "direction": "positive | negative | mixed | unclear",
  "confidence": 0.0
}
```

## 13.9 PolicyRegulatorySignal

```json
{
  "policy_id": "policy_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "policy_type": "subsidy | restriction | approval | standard | tariff | anti_dumping | export_control | environmental | safety | tax | industry_plan",
  "issuer": "...",
  "effective_date": "...",
  "affected_entities": [],
  "affected_products": [],
  "impact_channels": ["demand", "supply", "cost", "capex", "risk"],
  "summary": "...",
  "confidence": 0.0
}
```

## 13.10 RiskFlag

```json
{
  "risk_id": "risk_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "risk_type": "policy | legal | customer | supplier | quality | safety | environmental | liquidity | accounting | management | competition | technology | geopolitical",
  "risk_summary": "...",
  "severity": "low | medium | high | critical",
  "time_horizon": "near_term | medium_term | long_term",
  "impact_channels": ["revenue", "margin", "cashflow", "valuation", "risk_premium"],
  "confidence": 0.0
}
```

## 13.11 CatalystItem

```json
{
  "catalyst_id": "cat_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "catalyst_type": "earnings | policy_meeting | investor_day | tender_result | product_launch | capacity_commissioning | court_date | approval_deadline | conference | lockup_expiry",
  "expected_date": "...",
  "description": "...",
  "potential_impact": "...",
  "confidence": 0.0
}
```

## 13.12 AnalystQuestion

```json
{
  "question_id": "q_xxx",
  "source_link_id": "link_xxx",
  "target_id": "target_xxx",
  "related_event_id": "evt_xxx",
  "question": "该订单金额是否有官方公告确认？",
  "reason": "当前来源提到客户和订单，但金额未披露。",
  "priority": "high | medium | low",
  "suggested_queries": [
    "某公司 某客户 订单 金额",
    "某公司 某客户 供货协议",
    "某客户 供应商 某产品"
  ],
  "status": "open | searched | answered | dismissed"
}
```

## 13.13 CoverageGap

CoverageGap 用来记录“这次应该知道但没有找到”的内容。

```json
{
  "gap_id": "gap_xxx",
  "target_id": "target_xxx",
  "gap_type": "missing_customer_confirmation | missing_amount | missing_date | missing_policy_detail | missing_official_source | missing_metric",
  "description": "发现订单线索，但没有找到订单金额。",
  "suggested_next_queries": [],
  "priority": "high | medium | low"
}
```

---

# 14. 多模型结果合并

系统支持多个模型对同一链接或同一任务进行分析。

合并不是简单投票，而是按任务类型分别合并。

## 14.1 单篇链接的合并对象

```json
{
  "source_link_id": "link_xxx",
  "model_outputs": [
    {
      "model_config_id": "qwen_plus",
      "output": {}
    },
    {
      "model_config_id": "deepseek_v4_pro",
      "output": {}
    }
  ]
}
```

合并后生成：

```json
{
  "merged_analysis_id": "merged_xxx",
  "source_link_id": "link_xxx",
  "decision": "save_structured",
  "overall_score": 78,
  "confidence": 0.66,
  "disagreement_level": "low | medium | high",
  "accepted_facts": [],
  "accepted_metrics": [],
  "accepted_events": [],
  "accepted_relations": [],
  "accepted_risks": [],
  "accepted_catalysts": [],
  "field_conflicts": [],
  "merge_trace": {}
}
```

## 14.2 合并权重

每个模型本次输出的有效权重由配置和输出质量共同决定。

```text
effective_weight =
  configured_model_weight
  × task_weight
  × schema_validity_score
  × evidence_locator_score
  × output_confidence
  × historical_feedback_score
  × independence_adjustment
```

其中：

```text
configured_model_weight：配置给定
task_weight：该模型在该任务的配置权重
schema_validity_score：JSON 和 schema 是否通过
evidence_locator_score：是否能定位到输入段落
output_confidence：模型输出置信度
historical_feedback_score：历史反馈得分
independence_adjustment：模型底座或供应商相似度修正
```

## 14.3 决策合并

```text
save_structured = 1.0
link_only       = 0.5
skip            = 0.0
```

```text
merged_decision_score =
  Σ decision_value_i × effective_weight_i
  / Σ effective_weight_i
```

决策规则配置：

```yaml
merge_rules:
  save_structured:
    min_decision_score: 0.65
    min_overall_score: 70

  link_only:
    min_decision_score: 0.35

  conflict:
    trigger_when:
      - decision_gap_too_large
      - key_event_conflict
      - key_field_conflict
```

## 14.4 事件合并

事件合并步骤：

```text
1. event_type 归一
2. 公司、客户、供应商、产品名称归一
3. 日期、金额、产能、销量单位归一
4. 相似事件聚类
5. 字段级合并
6. 冲突字段保留
```

字段合并策略：

| 字段 | 合并方法 |
|---|---|
| event_type | 加权多数 |
| subject | 实体归一后加权多数 |
| counterparty | 实体归一后加权多数 |
| amount | 单位标准化后加权中位数 |
| date | 明确日期优先，模糊日期保留范围 |
| impact_direction | 加权投票 |
| impact_channels | 并集合并，低权重项降置信 |
| confidence | 加权计算 |
| conflict | 记录字段冲突 |

## 14.5 关系合并

关系型记录按以下字段去重：

```text
subject_entity
relation_type
object_entity
product
period
source_link_id
```

例如多个模型都抽取：

```text
某公司 → customer_of → 某客户
```

则合并为一条 RelationRecord，提高置信度。

如果模型对关系方向有冲突：

```text
A 说：某公司向客户供货
B 说：某客户向公司供货
```

则标记：

```json
{
  "conflict_type": "relation_direction_conflict",
  "resolution_status": "needs_arbitration"
}
```

---

# 15. 调用治理：减少无效模型调用

系统的费用控制不是“默认用什么模型”，而是**从调用机制上减少无效调用**。

## 15.1 调用治理策略

```yaml
call_governance:
  max_model_calls_per_run: 30
  max_parallel_model_groups_per_run: 10
  max_tokens_per_link: 6000
  max_selected_passages_per_link: 8
  batch_serp_triage: true
  reuse_existing_analysis: true
  reuse_same_content_hash: true
  reuse_same_canonical_url: true
  bundle_extraction: true
  early_stop_enabled: true
```

## 15.2 触发条件

```yaml
model_call_trigger:
  call_model_when:
    - rule_score_between: [40, 90]
    - high_value_keyword_matched: true
    - source_type in ["official", "exchange", "regulator", "company", "industry"]
    - contains_metric_or_amount: true
    - contains_relation_signal: true
    - coverage_gap_related: true

  skip_model_when:
    - duplicate_canonical_url_analyzed: true
    - same_content_hash_analyzed: true
    - title_snippet_low_relevance: true
    - same_event_high_confidence_already_exists: true
```

这里的 `skip_model_when` 是机制，不是限制信息读取。系统仍可以记录链接，只是不做重复模型分析。

## 15.3 调用复用

```text
同一链接不同 query 命中 → 复用同一模型结果
同一正文指纹重复 → 复用同一模型结果
同一事件多来源重复 → 对新来源只做补充字段抽取
同一目标同一日期批量分析 → 复用 Target Profile 和 Prompt 前缀
同一搜索结果摘要初筛 → 批量送模型
```

## 15.4 输出控制

为控制输出 token，模型输出要求：

```text
结构化 JSON
短字段
枚举值
少写长解释
长解释只放 analyst_summary
每个对象限制数量
低置信对象可进入 coverage_gaps 或 analyst_questions
```

示例配置：

```yaml
output_limits:
  max_facts: 10
  max_metrics: 10
  max_events: 5
  max_relations: 10
  max_risks: 5
  max_catalysts: 5
  max_questions: 8
  max_summary_chars: 500
```

---

# 16. 存储设计

## 16.1 数据库选择

建议用 PostgreSQL 起步。结构化文本检索可以用 PostgreSQL 全文索引；如果需要语义检索，可以只对 `brief`、`fact_statement`、`event.summary`、`risk_summary`、`analyst_question` 等结构化短文本做 embedding。

## 16.2 核心表

### target_profile

```sql
CREATE TABLE target_profile (
  id TEXT PRIMARY KEY,
  target_type TEXT,
  canonical_name TEXT,
  aliases JSONB,
  products JSONB,
  business_segments JSONB,
  customers JSONB,
  suppliers JSONB,
  competitors JSONB,
  upstream_terms JSONB,
  downstream_terms JSONB,
  metadata JSONB,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);
```

### search_run

```sql
CREATE TABLE search_run (
  id TEXT PRIMARY KEY,
  target_id TEXT,
  task_profile JSONB,
  budget_profile JSONB,
  query_plan_version TEXT,
  model_policy_version TEXT,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  status TEXT,
  summary JSONB
);
```

### search_query

```sql
CREATE TABLE search_query (
  id TEXT PRIMARY KEY,
  search_run_id TEXT,
  query_text TEXT,
  query_family TEXT,
  priority_score NUMERIC,
  language TEXT,
  region TEXT,
  expected_value_reason JSONB,
  executed BOOLEAN,
  created_at TIMESTAMP
);
```

### source_link

```sql
CREATE TABLE source_link (
  id TEXT PRIMARY KEY,
  search_run_id TEXT,
  query_id TEXT,
  provider TEXT,
  rank INT,
  title TEXT,
  snippet TEXT,
  url TEXT,
  canonical_url TEXT,
  domain TEXT,
  source_name TEXT,
  source_type TEXT,
  document_type TEXT,
  publish_time_guess TIMESTAMP,
  retrieved_at TIMESTAMP,
  access_profile_id TEXT,
  content_hash TEXT,
  simhash TEXT,
  read_status TEXT,
  triage_score NUMERIC,
  triage_decision TEXT,
  metadata JSONB
);
```

### link_read_attempt

```sql
CREATE TABLE link_read_attempt (
  id TEXT PRIMARY KEY,
  source_link_id TEXT,
  access_profile_id TEXT,
  read_status TEXT,
  http_status INT,
  content_type TEXT,
  content_length INT,
  extracted_title TEXT,
  extracted_publish_time TIMESTAMP,
  content_hash TEXT,
  selected_passage_count INT,
  failure_reason TEXT,
  created_at TIMESTAMP
);
```

### model_run

```sql
CREATE TABLE model_run (
  id TEXT PRIMARY KEY,
  source_link_id TEXT,
  task_name TEXT,
  call_mode TEXT,
  provider_type TEXT,
  provider TEXT,
  model_name TEXT,
  model_config_id TEXT,
  model_policy_version TEXT,
  prompt_version TEXT,
  schema_version TEXT,
  input_chars INT,
  input_tokens INT,
  output_tokens INT,
  reasoning_tokens INT,
  cached_tokens INT,
  estimated_cost NUMERIC,
  latency_ms INT,
  status TEXT,
  error_type TEXT,
  error_message TEXT,
  provider_request_id TEXT,
  created_at TIMESTAMP
);
```

### model_output

```sql
CREATE TABLE model_output (
  id TEXT PRIMARY KEY,
  model_run_id TEXT,
  source_link_id TEXT,
  output_json JSONB,
  schema_valid BOOLEAN,
  validation_errors JSONB,
  decision TEXT,
  overall_score NUMERIC,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### merged_analysis

```sql
CREATE TABLE merged_analysis (
  id TEXT PRIMARY KEY,
  source_link_id TEXT,
  target_id TEXT,
  decision TEXT,
  overall_score NUMERIC,
  confidence NUMERIC,
  disagreement_level TEXT,
  merge_method TEXT,
  model_outputs JSONB,
  field_conflicts JSONB,
  created_at TIMESTAMP
);
```

### analysis_brief

```sql
CREATE TABLE analysis_brief (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  one_sentence TEXT,
  what_happened TEXT,
  why_it_matters TEXT,
  affected_business_lines JSONB,
  impact_channels JSONB,
  time_horizon TEXT,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### fact_item

```sql
CREATE TABLE fact_item (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  fact_type TEXT,
  fact_statement TEXT,
  entities JSONB,
  metrics JSONB,
  period TEXT,
  direction TEXT,
  evidence_locator JSONB,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### metric_observation

```sql
CREATE TABLE metric_observation (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  metric_name TEXT,
  metric_value NUMERIC,
  unit TEXT,
  period TEXT,
  scope JSONB,
  comparison JSONB,
  interpretation TEXT,
  impact_channels JSONB,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### event_card

```sql
CREATE TABLE event_card (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  event_type TEXT,
  event_date TIMESTAMP,
  summary TEXT,
  entities JSONB,
  metrics JSONB,
  impact JSONB,
  source_corroboration_status TEXT,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### relation_record

```sql
CREATE TABLE relation_record (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  subject_entity JSONB,
  relation_type TEXT,
  object_entity JSONB,
  qualifiers JSONB,
  evidence_locator JSONB,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### risk_flag

```sql
CREATE TABLE risk_flag (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  risk_type TEXT,
  risk_summary TEXT,
  severity TEXT,
  time_horizon TEXT,
  impact_channels JSONB,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### catalyst_item

```sql
CREATE TABLE catalyst_item (
  id TEXT PRIMARY KEY,
  merged_analysis_id TEXT,
  source_link_id TEXT,
  target_id TEXT,
  catalyst_type TEXT,
  expected_date TIMESTAMP,
  description TEXT,
  potential_impact TEXT,
  confidence NUMERIC,
  created_at TIMESTAMP
);
```

### analyst_question

```sql
CREATE TABLE analyst_question (
  id TEXT PRIMARY KEY,
  source_link_id TEXT,
  target_id TEXT,
  related_event_id TEXT,
  question TEXT,
  reason TEXT,
  priority TEXT,
  suggested_queries JSONB,
  status TEXT,
  created_at TIMESTAMP
);
```

### coverage_gap

```sql
CREATE TABLE coverage_gap (
  id TEXT PRIMARY KEY,
  search_run_id TEXT,
  target_id TEXT,
  gap_type TEXT,
  description TEXT,
  suggested_next_queries JSONB,
  priority TEXT,
  status TEXT,
  created_at TIMESTAMP
);
```

---

# 17. 本地校验

模型输出进入数据库前，需要做本地校验。

## 17.1 Schema 校验

```text
JSON 是否合法
字段是否齐全
枚举值是否合法
数字、日期、单位是否可解析
数组长度是否超过限制
```

## 17.2 Evidence Locator 校验

因为不存原文，模型输出需要绑定临时输入段落的 `passage_id`。

```json
{
  "evidence_locator": {
    "passage_id": "p3",
    "section": "正文第5段"
  }
}
```

校验：

```text
passage_id 是否存在
fact / metric / event 是否能从该 passage 中支持
金额、日期、客户、产品是否出现在该 passage 中
```

## 17.3 关系方向校验

例如：

```text
A 向 B 供货
A 从 B 采购
A 中标 B 项目
B 选择 A 为供应商
```

需要尽量标准化为：

```text
A supplier_of B
B customer_of A
A contractor_of B
```

---

# 18. 运行流程

```text
1. 输入目标和任务

2. 加载 Target Profile

3. 根据 Analyst Intelligence Taxonomy 选择 query family

4. Query Planner 生成候选 query

5. Query Scoring 筛选高价值 query

6. 调用搜索源，得到 search hits

7. 保存 SourceLink 和 SearchHit 元数据

8. URL、标题、摘要、内容指纹去重

9. Search Hit Triage 决定 read / link_record_only / skip_for_now

10. Link Reader 读取搜索结果 URL

11. Content Preprocessor 临时清洗正文，提取相关段落和表格

12. Model Call Planner 判断是否需要模型、用哪种 call_mode

13. 调用配置的一个或多个模型

14. 校验 JSON、schema、evidence locator、关系方向

15. 多模型结果合并

16. 保存结构化结果：
    brief / facts / metrics / events / relations / risks / catalysts / questions / gaps

17. 丢弃临时正文

18. 生成 Batch Report

19. 分析员 Agent 查询结果
```

---

# 19. Batch Report

```json
{
  "search_run_id": "run_xxx",
  "target": "某公司",
  "time_window": "30d",
  "summary": {
    "queries_generated": 76,
    "queries_executed": 58,
    "search_hits": 580,
    "unique_source_links": 312,
    "links_read": 72,
    "links_model_analyzed": 24,
    "model_calls": 31,
    "parallel_ensemble_calls": 4,
    "fallback_calls": 3,
    "cached_or_reused_results": 19,
    "estimated_model_cost": 3.82
  },
  "structured_outputs": {
    "briefs": 18,
    "facts": 64,
    "metrics": 31,
    "events": 14,
    "relations": 27,
    "risks": 8,
    "catalysts": 5,
    "analyst_questions": 22,
    "coverage_gaps": 7
  },
  "top_events": [
    {
      "event_id": "evt_xxx",
      "summary": "...",
      "impact_channels": ["revenue"],
      "confidence": 0.68,
      "source_url": "..."
    }
  ],
  "top_relations": [
    {
      "relation_type": "customer_of",
      "subject": "某公司",
      "object": "某客户",
      "confidence": 0.74
    }
  ],
  "call_efficiency": {
    "deduplicated_links": 128,
    "reused_existing_analysis": 19,
    "batch_triage_saved_calls_estimate": 47,
    "passage_selection_saved_tokens_estimate": 620000
  }
}
```

---

# 20. Analyst Agent API

## 20.1 collect_intelligence

```python
collect_intelligence(
    target_id="company_xxx",
    task_profile={
        "focus": ["operating_update", "customer_change", "policy", "risk"],
        "time_window": "30d"
    },
    model_policy_version="model_policy_v0.3",
    query_plan_version="query_plan_v0.3"
)
```

## 20.2 get_recent_events

```python
get_recent_events(
    target_id="company_xxx",
    since="30d",
    event_types=["major_order", "policy_change", "customer_change"],
    min_confidence=0.4
)
```

## 20.3 get_metric_observations

```python
get_metric_observations(
    target_id="industry_xxx",
    metrics=["价格", "库存", "开工率", "产能利用率"],
    since="60d"
)
```

## 20.4 get_relations

```python
get_relations(
    target_id="company_xxx",
    relation_types=["customer_of", "supplier_of", "partner_of"],
    since="180d"
)
```

## 20.5 search_facts

```python
search_facts(
    target_id="company_xxx",
    query="订单 客户 金额 收入",
    filters={
        "fact_type": ["order", "customer", "sales"],
        "since": "90d"
    }
)
```

## 20.6 get_risks

```python
get_risks(
    target_id="company_xxx",
    since="90d",
    severity=["medium", "high", "critical"]
)
```

## 20.7 get_catalysts

```python
get_catalysts(
    target_id="company_xxx",
    from_date="2026-06-09",
    to_date="2026-12-31"
)
```

## 20.8 get_analyst_questions

```python
get_analyst_questions(
    target_id="company_xxx",
    priority="high",
    status="open"
)
```

## 20.9 explain_source_analysis

```python
explain_source_analysis(
    source_link_id="link_xxx"
)
```

返回：

```json
{
  "source": {
    "title": "...",
    "url": "...",
    "source_type": "...",
    "publish_time": "..."
  },
  "why_selected": "...",
  "models_used": [],
  "merged_decision": "...",
  "facts": [],
  "metrics": [],
  "events": [],
  "relations": [],
  "risks": [],
  "questions": []
}
```

---

# 21. 配置文件设计

```text
config/
  access_profiles.yaml
  search_providers.yaml
  target_profiles.yaml
  analyst_taxonomy.yaml
  query_families.yaml
  query_scoring.yaml
  source_packs.yaml
  model_registry.yaml
  model_policies.yaml
  merge_policy.yaml
  call_governance.yaml
  output_schema.yaml
  storage_policy.yaml
```

## 21.1 model_policies.yaml

```yaml
model_policies:
  version: model_policy_v0.3

  default_task_policy:
    bundle_extraction:
      call_mode: priority_fallback
      models:
        - model_id: qwen_plus
          priority: 1
          weight: 0.5
        - model_id: deepseek_v4_pro
          priority: 2
          weight: 0.5

    high_value_parallel_analysis:
      call_mode: parallel_ensemble
      models:
        - model_id: qwen_plus
          weight: 0.4
        - model_id: deepseek_v4_pro
          weight: 0.4
        - model_id: siliconflow_qwen
          weight: 0.2

    arbitration:
      call_mode: priority_fallback
      models:
        - model_id: deepseek_v4_pro
          priority: 1
        - model_id: openclaw_research
          priority: 2
```

## 21.2 call_governance.yaml

```yaml
call_governance:
  version: call_governance_v0.3

  budgets:
    max_model_calls_per_run: 30
    max_model_calls_per_source_link: 3
    max_parallel_model_groups_per_run: 5
    max_input_chars_per_model_call: 8000
    max_selected_passages_per_link: 8

  reuse:
    canonical_url_cache: true
    content_hash_cache: true
    same_event_cache: true
    same_query_batch_cache: true

  batching:
    serp_batch_triage: true
    serp_batch_size: 20

  extraction:
    prefer_bundle_extraction: true
    split_tasks_only_when:
      - selected_text_too_long
      - model_output_schema_failed
      - task_policy_requires_split

  early_stop:
    enabled: true
    stop_fallback_when:
      - schema_valid: true
      - confidence_gte: 0.75
      - required_objects_extracted: true
```

## 21.3 merge_policy.yaml

```yaml
merge_policy:
  version: merge_policy_v0.3

  decision_merge:
    method: weighted_vote
    decision_values:
      save_structured: 1.0
      link_only: 0.5
      skip: 0.0

  score_merge:
    method: weighted_median

  object_merge:
    facts: deduplicate_by_statement_entities_period
    metrics: deduplicate_by_metric_scope_period
    events: cluster_then_field_merge
    relations: deduplicate_by_subject_relation_object_qualifier
    risks: deduplicate_by_risk_type_summary
    catalysts: deduplicate_by_type_date_description

  conflict_rules:
    amount_conflict: keep_all_and_mark_conflict
    date_conflict: keep_range_or_mark_uncertain
    relation_direction_conflict: require_arbitration
    impact_direction_conflict: mark_mixed_or_unclear
```

## 21.4 storage_policy.yaml

```yaml
storage_policy:
  version: storage_policy_v0.3

  persist:
    source_links: true
    search_hits: true
    read_attempts: true
    content_hash: true
    model_runs: true
    model_outputs: true
    merged_analysis: true
    briefs: true
    facts: true
    metrics: true
    events: true
    relations: true
    risks: true
    catalysts: true
    analyst_questions: true
    coverage_gaps: true

  raw_content_persistence:
    html: false
    pdf: false
    full_text: false
    screenshot: false
```

---

# 22. 反馈与优化

## 22.1 分析师反馈

```json
{
  "feedback_id": "fb_xxx",
  "object_type": "event | fact | metric | relation | risk | catalyst | question",
  "object_id": "evt_xxx",
  "useful_for_analysis": true,
  "correct": true,
  "impact_direction_correct": false,
  "missing_fields": ["amount", "customer_confirmation"],
  "notes": "有价值，但需要官方来源确认。",
  "created_at": "..."
}
```

## 22.2 可优化对象

反馈用于优化：

```text
query family 权重
source pack 权重
source type 权重
模型任务权重
模型合并权重
触发条件
输出 schema
关系类型枚举
风险类型枚举
```

---

# 23. 最终运行示例

输入：

```yaml
target: 某公司
focus:
  - operating_update
  - customer_change
  - supply_chain
  - policy
  - risk
time_window: 30d
model_policy_version: model_policy_v0.3
```

执行：

```text
1. 生成 80 个候选 query
2. Query Scoring 后执行 55 个
3. 搜索得到 550 条结果
4. URL 去重后 280 条
5. Search Hit Triage 选择 75 条读取
6. Link Reader 读取 65 条成功
7. Passage Selection 生成短输入
8. Model Call Planner 计划 24 次模型调用
9. 其中 18 次 bundle_extraction，4 次 parallel_ensemble，2 次 arbitration
10. 保存结构化结果
```

输出：

```text
分析摘要 18 条
事实 64 条
指标 31 条
事件 14 条
关系记录 27 条
风险 8 条
催化剂 5 条
后续问题 22 条
覆盖缺口 7 条
```

---

# 24. 核心变化总结

```text
1. 模型选择完全配置化，不预设模型强弱和默认策略。

2. 多模型分析是内置能力，可以按任务、来源、事件重要性、配置版本启用。

3. 费用控制从“选便宜模型”改成“减少无效调用”：
   去重、缓存、批量初筛、段落选择、一次性 bundle 抽取、早停、复用已有结果。

4. 保存对象进一步贴近分析师：
   事实、指标、事件、关系、风险、催化剂、影响通道、后续问题、覆盖缺口。

5. 关系型数据作为一等对象保存，但用关系表实现，不做复杂图数据库。

6. 持久化以链接和结构化结果为主，不持久化原始网页正文。

7. Query Planner 围绕分析师需求分类生成搜索词，而不是简单公司名加关键词。
```

一句话总结：

> **MIC V0.3 是一个搜索结果驱动的分析师情报结构化系统：模型完全可配置，调用机制负责降噪和节省调用，输出围绕分析师决策所需的事实、指标、事件、关系、风险和催化剂组织。**

---

# 25. 参考资料

以下资料用于支持方案中的部分设计取向，例如模型接口兼容、上市公司信息披露需求、替代数据范围、OpenClaw Gateway 能力等：

1. DeepSeek API 文档：`https://api-docs.deepseek.com/zh-cn/`
2. DeepSeek JSON Output 文档：`https://api-docs.deepseek.com/zh-cn/guides/json_mode`
3. DeepSeek 上下文缓存文档：`https://api-docs.deepseek.com/zh-cn/guides/kv_cache`
4. 硅基流动 Chat Completions 文档：`https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions`
5. 阿里云 Model Studio Qwen OpenAI Compatible 文档：`https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope`
6. OpenClaw Gateway OpenAI HTTP API 文档：`https://docs.openclaw.ai/gateway/openai-http-api`
7. SEC EDGAR Search：`https://www.sec.gov/search-filings`
8. LSEG Alternative Data 介绍：`https://www.lseg.com/en/data-analytics/financial-data/alternative-data`
9. 中国证监会上市公司信息披露相关规则入口：`https://www.csrc.gov.cn/`
