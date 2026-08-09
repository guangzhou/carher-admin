"""忠实的 apply_patch —— 按 Codex 语义（*** Update File / @@ / 空格上下文行 / -删 / +增）。
之前极简版精确串替换匹配不上上下文，吞掉模型正确的补丁，害尺子低估 zerokey。"""
import re, os

def apply_patch(payload, SBX):
    results = []
    # 按文件段切
    for fm in re.finditer(r'\*\*\* (Add|Update|Delete) File: (.+?)(?=\n\*\*\* |\Z)', payload, re.S):
        action = fm.group(1)
        rest = fm.group(2)
        path = rest.split('\n', 1)[0].strip()
        full = path if path.startswith(SBX) else os.path.join(SBX, path.lstrip('/'))
        body = rest.split('\n', 1)[1] if '\n' in rest else ''
        # 去掉可能的 *** End Patch 尾
        body = re.split(r'\n\*\*\* End Patch', body)[0]
        if action == 'Add':
            lines = [l[1:] if l.startswith('+') else l for l in body.split('\n')]
            # 去掉 @@ 之类
            lines = [l for l in lines if not l.startswith('@@') and not l.startswith('***')]
            os.makedirs(os.path.dirname(full) or '.', exist_ok=True)
            open(full, 'w').write('\n'.join(lines).rstrip('\n') + '\n')
            results.append(f'added {path}')
            continue
        if action == 'Delete':
            if os.path.exists(full): os.remove(full)
            results.append(f'deleted {path}')
            continue
        # Update：按 hunk 应用。old=空格+减号行；new=空格+加号行
        if not os.path.exists(full):
            results.append(f'update failed: {path} 不存在'); continue
        src = open(full).read()
        old_lines, new_lines = [], []
        for l in body.split('\n'):
            if l.startswith('@@') or l.startswith('***') or l.startswith('*** '): continue
            if l.startswith('-'): old_lines.append(l[1:])
            elif l.startswith('+'): new_lines.append(l[1:])
            elif l.startswith(' '): old_lines.append(l[1:]); new_lines.append(l[1:])
            # 空行按上下文处理
            elif l == '': old_lines.append(''); new_lines.append('')
        old_block = '\n'.join(old_lines).strip('\n')
        new_block = '\n'.join(new_lines).strip('\n')
        if old_block and old_block in src:
            open(full, 'w').write(src.replace(old_block, new_block, 1))
            results.append(f'updated {path} ✓')
        else:
            # 上下文没精确命中 —— 退一步：只按纯 -/+ 行（不含上下文）再试
            pure_old = '\n'.join(l[1:] for l in body.split('\n') if l.startswith('-')).strip('\n')
            pure_new = '\n'.join(l[1:] for l in body.split('\n') if l.startswith('+')).strip('\n')
            if pure_old and pure_old in src:
                open(full, 'w').write(src.replace(pure_old, pure_new, 1))
                results.append(f'updated {path} ✓(纯增删)')
            else:
                results.append(f'update failed: {path} 上下文没命中')
    return '\n'.join(results) or 'apply_patch: 空'

if __name__ == '__main__':
    # 自测：Codex 风格上下文补丁
    import tempfile, shutil
    d = tempfile.mkdtemp()
    open(d+'/f.py','w').write("def g(p):\n    return p * (x / 100)\n\nprint('hi')\n")
    patch = ("*** Begin Patch\n*** Update File: f.py\n@@ def g(p):\n"
             "     # ctx\n-    return p * (x / 100)\n+    return p * ((100 - x) / 100)\n*** End Patch")
    # 注意上面 ctx 行在源码里没有，测退化路径
    print(apply_patch(patch, d))
    print(open(d+'/f.py').read())
    shutil.rmtree(d)
