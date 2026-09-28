#!/bin/bash
# 部署门禁的验证脚本。
#
# 第 107–158 轮本机进程表耗尽（fork 一律 EAGAIN），下面三件事一直没能跑过。
# 进程表恢复后，按这个顺序跑一遍即可；顺序是有讲究的，理由写在每一段上面。
#
# 用法：bash scripts/verify_deployment_gate.sh
#
# 只读、不改任何产品文件，也不启动服务：两段测试分别用 pytest 跑，不触网。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PY="${PY:-python3.12}"
FAIL=0

# 每个阶段先确认**确实收集到了用例**再跑。原因见文档 §66.1：
# pytest 在「一个用例都没收集到」时同样以 0 退出——路径写错、文件改名、
# 收集期报错都会让这一阶段打印成功却什么都没验证。
# 这里不写死任何期望数字（那等于把猜测量当断言），只要求：
#   1) 收集数 > 0；2) 实跑输出里 passed 数 > 0。
run_stage() {
    local label="$1"; shift
    local target="$1"; shift
    local collected n
    # 用「N tests collected」汇总行，而不是 tail -1——
    # 对目录收集时最后一行可能是用例 id 而非汇总，取不到的会是数字以外的东西。
    collected=$("$PY" -m pytest "$target" --collect-only -q 2>/dev/null \
        | grep -E "[0-9]+ tests? collected" | tail -1)
    n=$(printf "%s" "$collected" | grep -oE "^[0-9]+")
    # 收集不到汇总行（例如收集期报错）同样视为该阶段无效。
    if [ -z "$n" ] || [ "$n" -eq 0 ]; then
        echo "[verify] $label 未收集到任何用例（汇总行: ${collected:-<无>}）——该阶段无效，计为失败。"
        FAIL=1
        return 0
    fi
    echo "[verify] $label 已收集 $n 个用例。"
    local out
    out=$("$PY" -m pytest "$target" -q "$@" 2>&1)
    local rc=$?
    printf "%s" "$out" | tail -5
    if [ "$rc" -ne 0 ]; then
        FAIL=1
    fi
    # 即使退出码为 0，也要求输出里出现「N passed」且 N>0，
    # 防止全量被 skip/xfail 吞掉后仍报绿。
    local passed
    passed=$(printf "%s" "$out" | grep -oE "[0-9]+ passed" | head -1 | grep -oE "[0-9]+")
    if [ -z "$passed" ] || [ "$passed" -eq 0 ]; then
        echo "[verify] $label 退出码为 0，但输出里没有非零 passed——计为失败。"
        FAIL=1
    fi
    return 0
}

echo "== 1/3 test_preflight_database_branches.py（7 个用例，从未执行过）=="
# 这个文件不 fork bash，最便宜，先跑——它验证的是 check_database 七个分支，
# 也就是整轮审计里唯一真正的覆盖缺口（见文档 §52）。
run_stage "1/3 test_preflight_database_branches.py" tests/test_preflight_database_branches.py

echo
echo "== 2/3 test_preflight_guard_agreement.py（约 240 次 bash fork，必须单独跑）=="
# 这个文件每个参数组合都 fork 一次 bash（4x4x3x5 的 VNC 矩阵 + 8 个 worker 取值），
# 在本机进程表紧张时会自己先把环境拖垮、并让同批的其它测试随机报 EAGAIN。
# 所以它单独一段跑，不要和全量套件并在一起（见文档 §54.3）。
run_stage "2/3 test_preflight_guard_agreement.py" tests/test_preflight_guard_agreement.py

echo
echo "== 3/3 全量套件（不再排除任何测试文件）=="
# 这两个文件以前因 browser_register 在模块级硬 import camoufox 而必须 --ignore。
# Camoufox 现已改为守卫式导入，OAuth 预热测试也已跟上实现，因此第 3 段
# 直接运行完整 tests 目录，让收集数与 passed 守卫覆盖全部用例。
run_stage "3/3 全量套件" tests

echo
if [ "$FAIL" = "0" ]; then
    echo "[verify] 三段都通过。第 105 轮之后的所有改动至此才算有了全量回归。"
else
    echo "[verify] 有失败项，见上面的输出。"
fi
exit "$FAIL"
