#!/usr/bin/env python3
"""
自动更新 vps.yaml 的 rule-providers / rules 部分。

原理：
- port / proxy-providers / proxy-groups 这些基本不会变的部分，写死在 base_template.yaml 里
- rule-providers / rules 跟着 ACL4SSR 官方模板走，每次运行脚本时，
  用 subconverter 的 target=clash&expand=false 拉取最新结果，
  从里面把 rule-providers 和 rules 摘出来，拼回 base_template.yaml 后面
- 最终写出完整的 vps.yaml，可以配合 cron / GitHub Actions 定时跑，自动提交更新

用法：
    python3 update_config.py

依赖：
    pip install requests pyyaml --break-system-packages
"""

import sys
import requests
import yaml

# ========== 按需修改这几行 ==========
SUBCONVERTER_HOST = "https://api.wcc.best"          # 你的 subconverter 后端
ACL4SSR_INI = "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/config/ACL4SSR_Online_Full.ini"
# 这里的 url 只是给 subconverter 用来跑规则生成逻辑的占位节点，
# 规则/rule-providers 的生成跟节点内容无关，随便给一个能被正常解析的订阅/节点即可。
DUMMY_NODE_URL = "ss://YWVzLTEyOC1nY206dGVzdDEyM0AxMjcuMC4wLjE6ODM4OA=="
BASE_TEMPLATE = "base_template.yaml"                # 固定不变的 port/proxy-providers/proxy-groups
CUSTOM_RULES = "custom_rules.yaml"                  # 手动维护的自定义 rule-providers/rules（CustomDirect/CustomProxy/AWAvenueAds...）
OUTPUT_FILE = "vps.yaml"
# ====================================


def fetch_subconverter_yaml() -> dict:
    """请求 subconverter，expand=false 拿到 rule-providers + RULE-SET 引用形式的 rules"""
    params = {
        "target": "clash",
        "url": DUMMY_NODE_URL,
        "config": ACL4SSR_INI,
        "expand": "false",
        "new_name": "true",
        "emoji": "true",
        "insert": "false",   # 关键：不让后端插入它自己默认的额外规则/节点
        "list": "false",
        "sort": "false",
        "tfo": "false",
        "scv": "true",
        "fdn": "false",
    }

    headers = {
        # 伪装成正常浏览器请求，规避部分后端对默认 UA（python-requests/x.x）的拦截
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    resp = requests.get(f"{SUBCONVERTER_HOST}/sub", params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return yaml.safe_load(resp.text)


def extract_group_names(rules: list) -> list:
    """从 RULE-SET/MATCH/GEOIP 规则行里提取策略组名，保持首次出现顺序"""
    seen = {}
    for line in rules:
        parts = line.split(",")
        if parts[0] == "MATCH" and len(parts) >= 2:
            seen.setdefault(parts[1], None)
        elif parts[0] in ("RULE-SET", "GEOIP", "DOMAIN-KEYWORD", "DOMAIN-SUFFIX", "DOMAIN",
                           "IP-CIDR", "IP-CIDR6") and len(parts) >= 3:
            seen.setdefault(parts[2], None)
    return list(seen.keys())


def ensure_groups_exist(base_text: str, base_dict: dict, referenced_groups: list) -> str:
    """
    检查 rules 里引用的策略组，是不是都在 base_template 的 proxy-groups 里定义过。
    没有的话（比如 ACL4SSR 官方模板改了策略组名字），自动追加一个默认策略组，
    保证生成的 vps.yaml 始终是能被 Clash 正常加载的合法配置，而不是静默出错。
    """
    known = {g["name"] for g in base_dict.get("proxy-groups", [])}
    known |= {"DIRECT", "REJECT", "PASS"}
    missing = sorted(set(referenced_groups) - known)

    if not missing:
        return base_text

    print(f"!! 警告：检测到 {len(missing)} 个 rules 引用的策略组在 base_template.yaml 里不存在，"
          f"已自动补上默认分组，建议手动检查调整：{missing}", file=sys.stderr)

    extra_blocks = []
    for name in missing:
        extra_blocks.append(
            f"  - name: {name}\n"
            f"    type: select\n"
            f"    proxies:\n"
            f"      - 🚀 节点选择\n"
            f"      - DIRECT\n"
        )
    return base_text.rstrip() + "\n\n" + "\n".join(extra_blocks) + "\n"


def reorder_proxy_groups(proxy_groups: list, ordered_names: list) -> list:
    """按 rules 中策略组首次出现顺序重排 proxy-groups，未引用的组删除；固定特定组在最前"""
    PINNED = ["🚀 节点选择", "♻️ 自动选择", "🚀 手动切换"]
    name_to_group = {g["name"]: g for g in proxy_groups}
    ordered = []
    
    # 1. 固定核心组放最前
    for name in PINNED:
        if name in name_to_group:
            ordered.append(name_to_group.pop(name))
            
    # 2. 按 rules 顺序排列其余组
    for name in ordered_names:
        if name in name_to_group:
            ordered.append(name_to_group.pop(name))
            
    # 3. 递归保留被嵌套引用的策略组（防止 rules 没直接引用但 proxies 里用到的组被误删）
    i = 0
    while i < len(ordered):
        for p in ordered[i].get("proxies", []):
            if p in name_to_group:
                ordered.append(name_to_group.pop(p))
        i += 1
        
    return ordered


def build_final_config():
    remote = fetch_subconverter_yaml()

    if "rule-providers" not in remote or "rules" not in remote:
        print("!! subconverter 返回内容里没有 rule-providers/rules，可能是 expand 参数没生效，或者后端拒绝了请求", file=sys.stderr)
        print(remote, file=sys.stderr)
        sys.exit(1)

    # 规范化 remote rule-providers 的 path 文件名
    for name, provider in remote.get("rule-providers", {}).items():
        if "path" in provider:
            clean_name = name.split("(")[0].strip().replace(" ", "_")
            behavior = provider.get("behavior", "unknown")
            provider["path"] = f"./providers/{clean_name}_{behavior}.yaml"

    with open(BASE_TEMPLATE, "r", encoding="utf-8") as f:
        base_text = f.read()
    base_dict = yaml.safe_load(base_text)

    with open(CUSTOM_RULES, "r", encoding="utf-8") as f:
        custom = yaml.safe_load(f)

    # 关键点：自定义的 rule-providers/rules（CustomDirect/CustomProxy/AWAvenueAds...）
    # 永远来自 custom_rules.yaml，不会被 subconverter 拉回来的 ACL4SSR 结果覆盖，
    # 只是把两边的 rule-providers 字典合并、rules 列表按"自定义在前、ACL4SSR在后"拼接。
    merged_rule_providers = {**custom.get("rule-providers", {}), **remote["rule-providers"]}
    merged_rules = custom.get("rules", []) + remote["rules"]

    # 自愈校验：rules 里引用的策略组，必须都在 base_template 的 proxy-groups 里存在，
    # 否则自动补上默认分组，避免 ACL4SSR 改了策略组名字之后生成出损坏的配置。
    ordered_group_names = extract_group_names(merged_rules)
    base_text = ensure_groups_exist(base_text, base_dict, ordered_group_names)
    base_dict = yaml.safe_load(base_text)

    # 按 rules 中策略组首次出现顺序重排 proxy-groups
    base_dict["proxy-groups"] = reorder_proxy_groups(
        base_dict["proxy-groups"], ordered_group_names
    )

    # 把 base 部分（含重排后的 proxy-groups）和 rule-providers / rules 分别序列化再拼接
    base_tail = yaml.dump(
        {
            "proxy-providers": base_dict["proxy-providers"],
            "proxy-groups": base_dict["proxy-groups"],
        },
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    rules_tail = yaml.dump(
        {
            "rule-providers": merged_rule_providers,
            "rules": merged_rules,
        },
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )

    # port / socks-port / mode 等简单字段保留原始文本，proxy-providers / proxy-groups 用重排后的
    header_lines = []
    for key in ("port", "socks-port", "allow-lan", "mode", "log-level", "external-controller"):
        if key in base_dict:
            header_lines.append(f"{key}: {base_dict[key]}")
    final_text = "\n".join(header_lines) + "\n\n" + base_tail + "\n" + rules_tail

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(final_text)

    print(f"OK -> {OUTPUT_FILE}  (rule-providers: {len(merged_rule_providers)}, rules: {len(merged_rules)})")


if __name__ == "__main__":
    build_final_config()
