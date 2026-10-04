"""Source-linked product definitions for the ETF plans shown in the web app."""

ETF_DEFINITIONS = {
    "510300": {
        "category": "A 股大盘",
        "fund_name": "华泰柏瑞沪深300交易型开放式指数证券投资基金",
        "manager": "华泰柏瑞基金",
        "index_name": "沪深300指数 · 000300",
        "market": "沪深 A 股；基金在上交所以人民币交易",
        "summary": "跟踪沪深300指数。该指数选取沪深市场中规模较大、流动性较好的300只代表性证券，反映 A 股大盘股的整体表现。",
        "note": "指数点位、基金净值与场内成交价不是同一个数值；基金还可能存在跟踪误差和折溢价。",
        "sources": [
            {"label": "基金产品资料", "url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-11-24/510300_20251124_9WR7.pdf"},
            {"label": "沪深300指数编制方案", "url": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000300_Index_Methodology_cn.pdf"},
        ],
    },
    "512100": {
        "category": "A 股小盘",
        "fund_name": "南方中证1000交易型开放式指数证券投资基金",
        "manager": "南方基金",
        "index_name": "中证1000指数 · 000852",
        "market": "沪深 A 股；基金在上交所以人民币交易",
        "summary": "跟踪中证1000指数。该指数在中证800指数样本之外，选取规模偏小、流动性较好的1000只证券，反映 A 股小盘股的表现。",
        "note": "小盘股波动可能与大盘股不同；指数点位、基金净值与场内成交价也可能存在差异。",
        "sources": [
            {"label": "基金产品资料", "url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-23/512100_20260323_H1XE.pdf"},
            {"label": "中证1000指数编制方案", "url": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208175402-000852_Index_Methodology_cn.pdf"},
        ],
    },
    "513500": {
        "category": "美国大盘 · QDII",
        "fund_name": "博时标普500交易型开放式指数证券投资基金",
        "manager": "博时基金",
        "index_name": "标普500指数 · S&P 500",
        "market": "美国大盘股；基金在上交所以人民币交易",
        "summary": "通过跨境投资跟踪标普500指数，覆盖美国股票市场中约500家具有代表性的大型上市公司。",
        "note": "基金场内人民币价格不等于美元指数点位或基金净值，还受汇率、交易时差、跟踪误差和折溢价影响。",
        "sources": [
            {"label": "基金产品资料", "url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-26/513500_20260626_PDT5.pdf"},
            {"label": "标普500指数介绍", "url": "https://www.spglobal.com/spdji/en/indices/equity/sp-500/"},
            {"label": "场内溢价风险提示", "url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-26/513500_20260626_MAHV.pdf"},
        ],
    },
    "513100": {
        "category": "纳斯达克 · QDII",
        "fund_name": "纳斯达克100交易型开放式指数证券投资基金",
        "manager": "国泰基金",
        "index_name": "纳斯达克100指数 · Nasdaq-100",
        "market": "纳斯达克上市的非金融企业；基金在上交所以人民币交易",
        "summary": "通过跨境投资跟踪纳斯达克100指数。指数由纳斯达克交易所上市的100家较大型非金融企业构成，既包括美国企业，也包括其他国家的企业。",
        "note": "行业分布较集中；基金场内人民币价格还会受到汇率、交易时差、跟踪误差和折溢价影响，不能直接当作指数点位。",
        "sources": [
            {"label": "国泰基金产品页", "url": "https://e.gtfund.com/etrade/Jijin/view/id/513100"},
            {"label": "Nasdaq-100编制方法", "url": "https://indexes.nasdaq.com/docs/Methodology_NDX.pdf"},
            {"label": "场内溢价风险提示", "url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-01-09/513100_20260109_EPLN.pdf"},
        ],
    },
}
