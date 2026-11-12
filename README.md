# currency-api

采集 Mastercard、Visa、银联、JCB 官方参考汇率，提供兼容 `fawazahmed0/currency-api` 的 JSON 文件，可作为 GMPay（`GMWalletApp/epusdt`）的汇率数据源。

项目仓库：[AkinoKaede/currency-api](https://github.com/AkinoKaede/currency-api)。源代码位于 `main` 分支，生成的数据发布到 `data` 分支。项目不再生成月包、年包或 GitHub Releases 归档。

## 卡组织与方向

各卡组织独立维护报价、日期及来源，不跨机构补价。目录结构如下，每个方向都包含 `v1/currencies.json`、`v1/currencies/{币种}.json`、对应的 `.min.json` 和 `metadata.json`：

```text
mastercard/{direct,reverse}/
visa/{direct,reverse}/
unionpay/{direct,reverse}/
jcb/jpy/{direct,reverse}/
jcb/usd-buy/{direct,reverse}/
jcb/usd-mid/{direct,reverse}/
jcb/usd-sell/{direct,reverse}/
```

| 方向 | 换算公式 | `cny.usdt` 的含义 |
| --- | --- | --- |
| `direct` | 对应源数据中的同方向报价 | 1 CNY 对应多少 USD，再按 1:1 计为 USDT |
| `reverse` | 对应源数据中反方向报价的倒数 | 1 ÷ USD→CNY 原始报价，单位仍为 USDT/CNY |

两种方向均保持 currency-api 的字段含义，都是输入金额乘以汇率；不能直接把“1 USD 对应多少 CNY”填入 `cny.usdt`。两套报价不保证谁高谁低，不将其命名为买入价或卖出价。缺少所需方向的源报价时，省略该币对，不填零、不从其他机构补齐，也不通过中间币种推算。

`direct` 保留源数据的十进制精度。`reverse` 倒数使用十进制运算，截断至 100 位有效数字（`ROUND_DOWN`）；循环小数仍是近似值。USD 与配置中已启用的美元稳定币在两套输出中均固定 1:1，属于业务计价策略，不代表实时市场价格。

Mastercard 采集参数为 `bank_fee=0`，Visa 为 `fee=0`，银联读取官方发布文件。兼容层不额外添加平台加价或手续费。官方报价本身的变化会随成功采集自动更新。

## 美元稳定币

默认启用 **122 个币种键**，配置中另有 **5 个未启用的兼容条目**。完整名称和参考来源见 [`config/stablecoins.json`](config/stablecoins.json)。名单按 2026-11-12 整理，主要参考 [DefiLlama 的美元锚定资产数据](https://stablecoins.llama.fi/stablecoins?includePrices=true)，并加入 [USDT0](https://docs.usdt0.to/)、[桥接 USDC](https://www.circle.com/blog/bridged-usdc-standard) 和 [USDbC](https://help.coinbase.com/en/coinbase/getting-started/crypto-education/usd-base-coin)。运行时不会读取这些来源的市场价格，也不会自动把新发现的代币加入收款报价。

常用币种包括 USDT、USDC、DAI、USDS、USDe、USD1、USD0、USDtb、RLUSD、PYUSD、FDUSD、TUSD、USDP、GUSD、USDG、USDD、FRXUSD、GHO、crvUSD、LUSD、BOLD、AUSD；启用的全部 API 币种键如下：

```text
aegis-yusd apxusd astherus-usdf ausd avalon-usda avusd axis-usdx bold btcusd buck
cash cdxusd crvusd ctusd dai delpho-usdv dforce-usx dinari-usd-plus dkusd dlusd doc
dola dusd eusd fdusd feusd fidd frxusd ftusd fusd fxusd gate-usd gho gusd
gyroscope-gyd hermetica-usdh hex-trust-usdx hollar hubble-usdh iusd jupusd lavausd
lisusd lusd m mezo-musd mgusd mnee moveusd musd noon-usn open-usd ousd palm-pusd
plume-pusd pyusd reusd rlusd rwausd satusd sofid solomon-usdv spusd squsd
stables-usdr stablr-usd streamflow-usd-plus suiusde trusd tusd u unitas-usdu usat
usd-plus usd0 usd1 usda usdai usdat usdb usdbc usdbridge-usdb usdc usdc.e usdcbl
usdcv usdd usde usdf usdg usdgo usdh usdj usdk usdkg usdl usdm usdon usdp usdpt usdq
usdrif usds usdsc usdso usdst usdsui usdt usdt0 usdtb usdu usdv uspd usx uusd vai
valtorum-usdv veem-usdv xusd yusd yzusd zusd
```

每个币种都会生成独立的 `{code}.json` 与 `{code}.min.json`，同时出现在币种列表及所有具有 USD 报价的法币换算表中。例如，同一口径下：

```text
cny.usdt = cny.usdc = cny.usds = cny.pyusd = cny.usd
usd.usdt = usdt.usdc = usdc.pyusd = 1
```

币种键统一为小写。不按名称包含 `USD` 自动归类；sUSDe、sUSDS、sDAI 等份额型资产，以及 EURC、PAXG、XAUT 等非美元单位，不在默认平价名单内。部分同名代币使用发行方限定的键，例如 `gate-usd`、`aegis-yusd`、`mezo-musd`；`gyroscope-gyd` 用于稳定币，保留 `gyd` 作为原法币键。接口只标识报价单位，不标识链或合约，也不会自动使 GMPay 支持这些币种的收款。

`busd`、`fei`、`frax-legacy`、`susd`、`djed` 已记录但默认 `enabled: false`，不会生成平价报价。需要使用时显式启用。也可新增或移除其他条目；配置是完整名单，不会自动与默认名单合并。例如：

```json
{
  "schema_version": 1,
  "policy": "fixed_usd_parity",
  "coins": {
    "usdt": {"name": "Tether"},
    "usdc": {"name": "USD Coin"},
    "pyusd": {"name": "PayPal USD"},
    "busd": {"name": "Binance USD", "enabled": false}
  }
}
```

修改仓库的 `config/stablecoins.json` 后，后续采集与导出会自动使用新名单。也可指定另一份配置文件：

```sh
currency-api export-currency-api --provider mastercard --data-dir rates-data --stablecoins-config ./my-stablecoins.json
```

`fetch` 和 `merge-shards` 同样支持 `--stablecoins-config`。重新导出会清理该卡组织两种方向中已停用币种的文件和报价，不影响其他卡组织；要同步所有已发布数据，需要分别重新导出各数据源。元数据记录启用名单及其 SHA256，以便核对实际采用的计价配置。

## JCB 的口径

JCB 原表分为 JPY 表和 USD 表的 Buy、Mid、Sell 三列，不能静默混成一套报价：

- `jcb/usd-buy/direct/`、`jcb/usd-mid/direct/`、`jcb/usd-sell/direct/` 提供各列对应的 **CNY→USD→稳定币** 计价。选择哪列由业务决定，本项目不默认选 Mid。
- 这三套 `reverse/` 提供 **USD／稳定币→CNY** 等反向换算，使用源数据的 `rateNumerator` / `rateDenominator` 精确分数恢复原表数值，避免对截断后的 `rateData` 再取倒数。
- USD 表每列只提供一套报价，不是 Mastercard 等机构的独立双向矩阵。因此 JCB USD 的 `reverse/.../cny.json` 不含 `cny.usdt`，**不能作为 CNY 订单收 USDT 的 GMPay 数据源**；这个场景选择对应的 `direct/`。
- `jcb/jpy/direct/` 保留交易币种→JPY 报价；`reverse/` 提供其倒数。该表不推导 CNY→USD 交叉汇率，因此不适用于 CNY 订单收 USDT。JPY 源表是否由同日 USD 表重建会记录在元数据中。

## GMPay 使用方式

对于 **CNY 订单、按 Mastercard 报价收取 USDT**，在 GMPay 自动汇率模式下，将汇率接口设置为以下目录地址：

```text
https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/direct/v1/currencies/
```

目录地址末尾保留 `/`，不添加 `cny.json`。商户提交 `amount=100, currency=cny, token=usdt`，GMPay 自动请求 `cny.json`，读取 `cny.usdt`，计算 `100 × cny.usdt`。不要先换成 USDT 后仍以 `currency=cny` 提交，否则会二次换算。

示例响应节选（实际包含更多已启用稳定币；日期和 `0.14` 均为演示，不是实时报价）：

```json
{
  "date": "2026-11-12",
  "cny": {
    "cny": 1,
    "usd": 0.14,
    "usdt": 0.14,
    "usdc": 0.14,
    "dai": 0.14
  }
}
```

此例 100 CNY 对应 14 USDT。假设同日 USD→CNY 原始报价为 `7.2`，Mastercard `reverse` 口径则对应约 13.888889 USDT。收银台最终应显示 GMPay 返回的 `actual_amount`，其中可能包含金额精度处理及避免订单金额冲突的调整。收款资产、链、转账确认和回调由 GMPay 处理。

## jsDelivr 地址

以下地址使用 [jsDelivr GitHub 分支格式](https://www.jsdelivr.com/?docs=gh)，`@data` 指定数据分支。**生成文件发布到 `AkinoKaede/currency-api` 的公开 `data` 分支后才可访问**；本地生成不会自动上传。

| 卡组织 | direct 目录 | reverse 目录 |
| --- | --- | --- |
| Mastercard | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/reverse/v1/currencies/` |
| Visa | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/visa/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/visa/reverse/v1/currencies/` |
| 银联 | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/unionpay/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/unionpay/reverse/v1/currencies/` |
| JCB USD Buy | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-buy/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-buy/reverse/v1/currencies/` |
| JCB USD Mid | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-mid/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-mid/reverse/v1/currencies/` |
| JCB USD Sell | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-sell/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/usd-sell/reverse/v1/currencies/` |
| JCB JPY | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/jpy/direct/v1/currencies/` | `https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/jcb/jpy/reverse/v1/currencies/` |

以 Mastercard direct 为例：

```text
# CNY 换算表
https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/direct/v1/currencies/cny.json

# 支持币种列表：法币显示名称使用大写代码
https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/direct/v1/currencies.json

# 来源、日期、公式、精度和稳定币策略
https://cdn.jsdelivr.net/gh/AkinoKaede/currency-api@data/mastercard/direct/metadata.json

# GitHub Raw 对应文件
https://raw.githubusercontent.com/AkinoKaede/currency-api/data/mastercard/direct/v1/currencies/cny.json
```

`date` 是实际汇率日期，不是下载日期。采集失败不会伪造当天数据，旧文件保留原日期；旧日期补采不会覆盖新报价，同日修正会更新。CDN 和 GMPay 各有缓存，不应假设发布后立即生效。兼容文件只保留最新一套，历史参考数据仍可按下面的每日路径读取。

## 采集与发布

安装依赖和命令：

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps .
```

采集指定日期，成功保存时自动生成对应卡组织的兼容文件：

```sh
currency-api fetch --provider mastercard --start-date 2026-11-12 --data-dir rates-data
currency-api fetch --provider visa --start-date 2026-11-12 --data-dir rates-data
currency-api fetch --provider unionpay --start-date 2026-11-12 --data-dir rates-data
currency-api fetch --provider jcb --start-date 2026-11-12 --data-dir rates-data
```

省略日期时使用北京时间当天。也可使用 `python -m exchange_rates` 调用同一 CLI。已有本地数据时可离线重新导出，无需重新采集：

```sh
currency-api export-currency-api --provider mastercard --data-dir rates-data
currency-api export-currency-api --provider visa --data-dir rates-data
currency-api export-currency-api --provider unionpay --data-dir rates-data
currency-api export-currency-api --provider jcb-usd-buy --data-dir rates-data
currency-api export-currency-api --provider jcb-usd-mid --data-dir rates-data
currency-api export-currency-api --provider jcb-usd-sell --data-dir rates-data
currency-api export-currency-api --provider jcb-jpy --data-dir rates-data
```

GitHub Actions 中分别运行 `Fetch Mastercard`、`Fetch Visa`、`Fetch UnionPay`、`Fetch JCB` 工作流，成功数据发布到 `data` 分支。工作流使用 GitHub 自动提供的 `github.token`，不要求额外填写个人 Token；公开文件的 jsDelivr 和 GMPay 读取也不需要 Token。本地手动发布需要已有 Git 仓库、远端与 GitHub 认证，`--publish` 会推送到指定数据分支，数据目录必须为空。

月包、年包、归档下载及归档校验命令已移除，不再调用 GitHub Releases API，也不再运行归档定时工作流。

## 原始汇率与每日数据

`data` 分支中的 `name` 为 `mastercard`、`visa`、`unionpay`、`jcb-jpy`、`jcb-usd-buy`、`jcb-usd-mid` 或 `jcb-usd-sell`：

| 内容 | 路径 |
| --- | --- |
| 最新标准汇率 | `latest/name.json` |
| 每日标准汇率 | `history/YYYY/MM/name/YYYY-MM-DD.json` |
| 最新原始响应或分片索引 | `raw/latest/name.json` |
| 每日原始响应或分片索引 | `raw/history/YYYY/MM/name/YYYY-MM-DD.json` |
| 最新元数据 | `metadata/latest/name.json` |
| 每日元数据 | `metadata/history/YYYY/MM/name/YYYY-MM-DD.json` |

标准汇率的 `exchangeRateJson` 数组中，`transCur` 是输入币种，`baseCur` 是输出币种，`rateData` 表示 1 单位输入币种对应的输出数量。它与兼容接口顶层输入币种的方向一致。

Visa、Mastercard 的原始响应按索引中的 `parts_directory` 和 `parts` 读取分片，索引包含大小及 SHA256。银联和 JCB 的原始响应为单文件 JSON 封装。JCB USD 三份文件各自保留原表列的倒数，以及精确约分的 `rateNumerator`、`rateDenominator`；JPY 重建时使用同日 `JPY Sell / 交易币种 Buy`，截断到小数点后 3 位，元数据记录公式与来源。

单个原始文件超过 100 MiB 时使用 Git LFS；原始链接可能返回 LFS 指针。需要完整数据副本时，安装 Git LFS 后克隆数据分支：

```sh
git clone --single-branch --branch data https://github.com/AkinoKaede/currency-api.git rates-data
```

本项目保留每日数据但不打包归档；每日原始响应与 Git 历史仍会占用空间。参考汇率不保证覆盖每个日历日，也不保证等于真实银行卡最终账单。

## 许可

代码采用 **AGPL-3.0-only**，全文见 [LICENSE](LICENSE)。卡组织原始响应及汇率数据不套用代码许可，使用、转载或再分发须遵守对应原始来源的使用条件。
