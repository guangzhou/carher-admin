"""
快速排序、冒泡排序、折半查找 — 分别用栈和队列实现（迭代版）
"""
from collections import deque


# ─────────────────────────── 工具：partition ────────────────────────────

def _partition(arr: list, low: int, high: int) -> int:
    pivot = arr[high]
    i = low - 1
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


# ═══════════════════════════ 快速排序 ════════════════════════════════════

def quicksort_stack(arr: list) -> list:
    """快速排序 — 用栈模拟递归调用（DFS 顺序）"""
    stack = [(0, len(arr) - 1)]
    while stack:
        low, high = stack.pop()
        if low >= high:
            continue
        p = _partition(arr, low, high)
        stack.append((low, p - 1))
        stack.append((p + 1, high))
    return arr


def quicksort_queue(arr: list) -> list:
    """快速排序 — 用队列替代栈（BFS 顺序，先处理浅层子区间）"""
    queue: deque = deque([(0, len(arr) - 1)])
    while queue:
        low, high = queue.popleft()
        if low >= high:
            continue
        p = _partition(arr, low, high)
        queue.append((low, p - 1))
        queue.append((p + 1, high))
    return arr


# ═══════════════════════════ 冒泡排序 ════════════════════════════════════

def bubblesort_stack(arr: list) -> list:
    """冒泡排序 — 用栈管理待排序区间，每次弹出区间做一趟冒泡"""
    stack = [(0, len(arr) - 1)]
    while stack:
        low, high = stack.pop()
        if low >= high:
            continue
        swapped = False
        for i in range(low, high):
            if arr[i] > arr[i + 1]:
                arr[i], arr[i + 1] = arr[i + 1], arr[i]
                swapped = True
        if swapped:                     # 最大值已就位，缩小区间继续
            stack.append((low, high - 1))
    return arr


def bubblesort_queue(arr: list) -> list:
    """冒泡排序 — 用队列管理待排序区间（逻辑与栈版相同，入队替换入栈）"""
    queue: deque = deque([(0, len(arr) - 1)])
    while queue:
        low, high = queue.popleft()
        if low >= high:
            continue
        swapped = False
        for i in range(low, high):
            if arr[i] > arr[i + 1]:
                arr[i], arr[i + 1] = arr[i + 1], arr[i]
                swapped = True
        if swapped:
            queue.append((low, high - 1))
    return arr


# ═══════════════════════════ 折半查找 ════════════════════════════════════

def binary_search_stack(arr: list, target) -> int:
    """折半查找 — 用栈存储待搜索区间（每次只压入一个子区间）"""
    stack = [(0, len(arr) - 1)]
    while stack:
        low, high = stack.pop()
        if low > high:
            continue
        mid = (low + high) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            stack.append((mid + 1, high))
        else:
            stack.append((low, mid - 1))
    return -1


def binary_search_queue(arr: list, target) -> int:
    """折半查找 — 用队列存储待搜索区间（每次只入队一个子区间）"""
    queue: deque = deque([(0, len(arr) - 1)])
    while queue:
        low, high = queue.popleft()
        if low > high:
            continue
        mid = (low + high) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            queue.append((mid + 1, high))
        else:
            queue.append((low, mid - 1))
    return -1


# ═══════════════════════════ 测试 ═════════════════════════════════════════

if __name__ == "__main__":
    import random

    def run_sort_tests(fn, name):
        cases = [
            ([3, 1, 4, 1, 5, 9, 2, 6], [1, 1, 2, 3, 4, 5, 6, 9]),
            ([], []),
            ([1], [1]),
            ([2, 1], [1, 2]),
            (list(range(10, 0, -1)), list(range(1, 11))),
        ]
        for data, expected in cases:
            result = fn(data[:])
            assert result == expected, f"{name} FAIL: {data} → {result}"
        print(f"  {name}: OK")

    def run_search_tests(fn, name):
        arr = [1, 3, 5, 7, 9, 11, 13]
        assert fn(arr, 7)  == 3,  f"{name}: found wrong index"
        assert fn(arr, 1)  == 0,  f"{name}: first element"
        assert fn(arr, 13) == 6,  f"{name}: last element"
        assert fn(arr, 4)  == -1, f"{name}: missing element"
        assert fn([], 1)   == -1, f"{name}: empty array"
        print(f"  {name}: OK")

    print("── 快速排序 ──")
    run_sort_tests(quicksort_stack, "quicksort_stack")
    run_sort_tests(quicksort_queue, "quicksort_queue")

    print("── 冒泡排序 ──")
    run_sort_tests(bubblesort_stack, "bubblesort_stack")
    run_sort_tests(bubblesort_queue, "bubblesort_queue")

    print("── 折半查找 ──")
    run_search_tests(binary_search_stack, "binary_search_stack")
    run_search_tests(binary_search_queue, "binary_search_queue")

    print("\n全部通过 ✓")
