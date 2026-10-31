---
name: purchase-and-warranty
description: Locate authorized purchase evidence and distinguish warranty findings from missing information.
version: 1.0.0
---

# 购买与质保核验

先确认任务产品和具体设备，再按最少必要信息查询 `lookup_purchase`。查询只访问当前用户授权的订单与材料。

- 内部订单查无结果不等于没有质保。允许补充经销商凭证，记录仍未核验的渠道、购买日期和地区。
- 图片必须用 `inspect_image` 实际读取。文字可见不等于票据真实或设备归属已经证明；不清晰就请用户提供可读信息。
- 用 `check_warranty` 由后端核对渠道和适用政策。必须区分适用、不适用、信息不足；缺政策或名录时不能给确定承诺。
- 政策按销售地区、渠道与购买／事件时间匹配，不能按语言猜地区，也不能只取最新版。
- 产品变化后重新核对购买关联和质保结果。历史结论只作为过去的观察。
- 无法自动核验时，将必要证据与缺项纳入人工审查草案。不得要求用户提供密码、验证码或不必要的完整付款资料。

保修期限、官方授权关系及退款规则均来自当前有效业务记录，不写入本 Skill 作为通用常量。
