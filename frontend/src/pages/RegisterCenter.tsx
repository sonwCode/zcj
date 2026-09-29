import { Link } from 'react-router-dom'
import { Play, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'

export default function RegisterCenter() {
  return (
    <div className="space-y-4">
      <Card className="p-6">
        <div className="flex flex-col gap-5 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-xl font-semibold text-[var(--text-primary)]"><Play className="h-5 w-5 text-[var(--accent)]" />注册中心</div>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--text-muted)]">这里保留昨天压缩包的注册中心入口；实际任务表单沿用当前 zcj 工作台，认证和后端契约保持一致。</p>
          </div>
          <Button asChild><Link to="/register"><Play className="mr-2 h-4 w-4" />创建注册任务</Link></Button>
        </div>
      </Card>
      <Card className="p-5">
        <div className="flex items-start gap-3">
          <ShieldCheck className="mt-0.5 h-5 w-5 text-emerald-400" />
          <div><div className="font-medium text-[var(--text-primary)]">注册引擎边界已保留</div><div className="mt-1 text-sm text-[var(--text-muted)]">OAuth、手机号、邮箱、验证码和执行器选项继续由当前工作台处理，不用旧页面覆盖现有能力。</div></div>
        </div>
      </Card>
    </div>
  )
}
