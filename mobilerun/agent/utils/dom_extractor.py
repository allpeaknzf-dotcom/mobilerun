"""DOM extraction script and element normalization for Web/H5 platforms.

Inject DOM_EXTRACTOR_JS into a Playwright page to collect interactive elements,
then use normalize_element() to map the output to IndexedFormatter-compatible format.
"""

from __future__ import annotations

DOM_EXTRACTOR_JS = """
() => {
  const INTERACTIVE = 'a[href],button,input:not([type="hidden"]),select,textarea,'
    + '[role="button"],[role="link"],[role="textbox"],[role="searchbox"],'
    + '[role="combobox"],[role="listbox"],[role="menuitem"],[role="tab"],'
    + '[role="switch"],[role="checkbox"],[role="radio"],'
    + '[onclick],[tabindex]:not([tabindex="-1"]),summary,details,label,legend,'
    + 'div[class*="btn"],div[class*="button"],div[class*="confirm"],div[class*="cancel"],div[class*="operate"],div[class*="action"],'
    + 'span[class*="btn"],span[class*="confirm"],span[class*="cancel"],span[class*="operate"],span[class*="action"]';

  const results = [];
  let idx = 0;
  const squashText = (value) => (value || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  const extractLabel = (el) => {
    const attrLabel = el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.getAttribute('alt')
      || '';
    if (attrLabel) return squashText(attrLabel);

    let cleaned = '';
    try {
      const clone = el.cloneNode(true);
      clone.querySelectorAll(
        '[role="menu"],[role="listbox"],[role="tree"],[role="dialog"],script,style'
      ).forEach(node => node.remove());
      cleaned = squashText(clone.innerText || clone.textContent || '');
    } catch (e) {}
    if (cleaned) return cleaned;
    return squashText(el.innerText || el.textContent || '');
  };

  document.querySelectorAll(INTERACTIVE).forEach(el => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    if (rect.bottom < 0 || rect.top > window.innerHeight) return;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return;
    if (parseFloat(style.opacity) === 0) return;

    const label = extractLabel(el);

    let tag = el.tagName.toLowerCase();
    let etype = el.getAttribute('type')
             || el.getAttribute('role')
             || tag;

    results.push({
      index: idx,
      tag: tag,
      type: etype,
      role: el.getAttribute('role') || '',
      text: label,
      bounds: Math.round(rect.left) + ','
            + Math.round(rect.top) + ','
            + Math.round(rect.right) + ','
            + Math.round(rect.bottom),
      clickable: true,
      checked: el.checked !== undefined ? el.checked : null,
      disabled: el.disabled || null,
      href: el.getAttribute('href') || null,
      aria_expanded: el.getAttribute('aria-expanded') || null,
      aria_current: el.getAttribute('aria-current') || null,
      descendant_menuitem_count: el.querySelectorAll('[role="menuitem"]').length,
      descendant_menu_count: el.querySelectorAll('[role="menu"]').length,
      input_type: el.getAttribute('type') || null,
    });

    idx += 1;
  });

  return results;
}
"""


def normalize_element(element: dict) -> dict:
    """Map DOM extractor output to IndexedFormatter-compatible fields.

    IndexedFormatter expects:
        className, resourceId, text, bounds, checkedState, children

    DOM extractor provides:
        index, tag, type, text, bounds, clickable, checked, disabled, href, input_type
    """
    tag = element.get("tag", "")
    etype = element.get("type", "")

    # Build className like "button:submit", "a:link", "input:text"
    if etype and etype != tag:
        class_name = f"{tag}:{etype}"
    else:
        class_name = tag

    # checkedState: "" | "checked" | "disabled"
    checked_state = ""
    if element.get("checked") is True:
        checked_state = "checked"
    if element.get("disabled") is True:
        checked_state = (
            "disabled" if not checked_state else f"{checked_state}|disabled"
        )

    # text: append supplementary info (href, input_type)
    text = element.get("text", "")
    suffixes = []
    href = element.get("href")
    if href and href != text:
        suffixes.append(href)
    input_type = element.get("input_type")
    if input_type and input_type not in ("text", etype):
        suffixes.append(input_type)
    if suffixes:
        suffix = " [" + ", ".join(suffixes) + "]"
        text = (text + suffix) if text else ", ".join(suffixes)

    # Convert bounds string "l,t,r,b" to boundsInScreen dict
    bounds_str = element.get("bounds", "")
    bounds_parts = [int(float(p)) for p in bounds_str.split(",")] if bounds_str else [0, 0, 0, 0]
    while len(bounds_parts) < 4:
        bounds_parts.append(0)

    return {
        "index": element.get("index"),
        "className": class_name,
        "resourceId": "",
        "text": text,
        "bounds": bounds_str,
        "boundsInScreen": {
            "left": bounds_parts[0],
            "top": bounds_parts[1],
            "right": bounds_parts[2],
            "bottom": bounds_parts[3],
        },
        "checkedState": checked_state,
        "role": element.get("role", ""),
        "href": href or "",
        "ariaExpanded": element.get("aria_expanded") or "",
        "ariaCurrent": element.get("aria_current") or "",
        "descendantMenuItemCount": int(
            element.get("descendant_menuitem_count") or 0
        ),
        "descendantMenuCount": int(element.get("descendant_menu_count") or 0),
        "children": [],
    }
