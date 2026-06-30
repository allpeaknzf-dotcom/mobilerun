"""PEL Layer 0 — YAML 页面描述生成器。

把自动发现的元素清单写成 ``.mobilerun/pages/auto_<page_id>.yaml``，
非技术人员可直接编辑。文件已存在（用户编辑过）时只做增量合并，
保留用户手动添加的选择器与元素。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("mobilerun")


class YAMLGenerator:
    """元素清单 → .yaml 文件（带用户编辑合并）。"""

    def __init__(self, yaml_dir: str = ".mobilerun/pages/") -> None:
        self.yaml_dir = yaml_dir

    def build_yaml_data(
        self,
        page_id: str,
        url_patterns: list[str],
        title_patterns: list[str],
        key_element_texts: list[str],
        platform: str,
        viewport: str,
        elements: dict,
        generated_at: str,
    ) -> dict:
        """构造将写入 YAML 的统一结构 dict。"""
        return {
            "page_id": page_id,
            "url_patterns": url_patterns,
            "title_patterns": title_patterns,
            "key_element_texts": key_element_texts,
            "platform": platform,
            "viewport": viewport,
            "auto_generated": True,
            "generated_at": generated_at,
            "match_threshold": 60,
            "max_cached_hits": 5,
            "elements": elements,
        }

    def write(self, page_id: str, yaml_data: dict) -> str:
        """写入 YAML 文件。已存在则合并用户编辑。返回文件路径。"""
        import yaml

        os.makedirs(self.yaml_dir, exist_ok=True)
        path = os.path.join(self.yaml_dir, f"{page_id}.yaml")

        existing = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to read existing YAML %s: %s", path, e)
                existing = {}

        if existing:
            self._merge_user_edits(yaml_data, existing)

        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(
                    yaml_data, f, allow_unicode=True, default_flow_style=False,
                    sort_keys=False,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to write page YAML %s: %s", path, e)
        return path

    @staticmethod
    def _merge_user_edits(auto_data: dict, existing: dict) -> None:
        """把用户在旧 YAML 里的手动编辑合并进新生成的数据。

        - elements：用户自定义选择器优先，保留用户手动新增的元素
        - key_element_texts / title_patterns：保留用户已有的并集
        """
        # 标量/列表类字段：用户编辑过的 title/threshold 等优先保留
        for key in ("match_threshold", "max_cached_hits"):
            if key in existing:
                auto_data[key] = existing[key]

        for key in ("key_element_texts", "title_patterns", "url_patterns"):
            user_list = existing.get(key) or []
            auto_list = auto_data.get(key) or []
            merged = list(user_list) + [x for x in auto_list if x not in user_list]
            auto_data[key] = merged

        # elements 合并
        auto_elements = auto_data.get("elements", {})
        user_elements = existing.get("elements", {})
        for name, user_el in user_elements.items():
            if name in auto_elements:
                user_sel = user_el.get("detected_selectors", []) or []
                auto_sel = auto_elements[name].get("detected_selectors", []) or []
                auto_elements[name]["detected_selectors"] = user_sel + [
                    s for s in auto_sel if s not in user_sel
                ]
                # 保留用户调整过的 post_action_wait / required / invalidate_after
                for fld in (
                    "post_action_wait", "required", "cache_ttl", "invalidate_after"
                ):
                    if fld in user_el:
                        auto_elements[name][fld] = user_el[fld]
            else:
                auto_elements[name] = user_el
        auto_data["elements"] = auto_elements
