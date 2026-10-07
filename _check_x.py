import sys
sys.path.insert(0, 'evaluation')
import faithfulness as F

ev = ["Anubis appears with a human body and a dark canid's head. "
      "Anubis is bare-chested but wears a headdress from ancient Egypt."]
cases = [
    ("翻译①人体+犬头",
     "Anubis 是一个近似于神话中的 Anubis 的形象，具有人类的身体和黑暗犬类的头。", True),
    ("翻译②裸体+头饰",
     "Anubis 裸体但戴着来自古埃及的头饰。", True),
    ("翻译③带修饰",
     "Anubis 穿着来自古埃及的头饰，且是裸体的。", True),
    ("幻觉：翅膀",
     "Anubis 具有人类的身体和翅膀。", False),
    ("幻觉：猫头",
     "Anubis 具有人类的身体和一个猫的头。", False),
    ("幻觉：翅膀+臂章",
     "Anubis 具有人类的身体、翅膀并戴着臂章。", False),
    ("幻觉：钢铁激光",
     "Anubis 由钢铁构成，能够发射激光。", False),
    ("幻觉：编数值",
     "Anubis 的破坏力是 A 级，速度是 C。", False),
    ("幻觉：无关",
     "Tusk 是能够操控火焰的替身。", False),
]
bad = 0
for n, s, exp in cases:
    ok, _ = F.sentence_supported(s, ev)
    flag = "OK " if ok == exp else "?? "
    if ok != exp:
        bad += 1
    print(f"  {flag}{n:16s} {ok}  期望{exp}")
print(f"\n失败 {bad}/{len(cases)}")
