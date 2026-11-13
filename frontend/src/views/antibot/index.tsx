import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/antibot'

const columns = [
  '策略编号',
  '验证码类型',
  '识别服务',
  '触发条件',
  '等待时长',
  '重试上限',
  '降级方案',
  '优先级',
  '策略状态',
]
const actions = ['提交校验', '启用策略', '停用策略']
const statuses = ['草稿', '待校验', '已启用', '已停用']
const captchaTypes = ['滑块', '点选', '字符', '智能无感', '拼图']
const recognitionServices = ['本地识别', '打码平台A', '打码平台B', '人工兜底']
const fallbackPlans = ['切换备用识别服务', '人工接管', '跳过采集', '延迟重试', '无']
const conditionOperators = [
  { code: 'http_status', label: 'HTTP 状态码等于' },
  { code: 'header_contains', label: '响应头包含' },
  { code: 'body_contains', label: '响应正文包含' },
  { code: 'risk_level_gte', label: '风险等级大于等于' },
]

type Row = Record<string, string | number | null> & {
  executable?: boolean
  reasons?: string[]
  parsed?: Record<string, unknown>
  触发条件?: Array<{ operator: string; value: string | number }>
}

type Candidate = {
  rank: number
  decision: string
  reason: string
  strategy_id: number
  策略编号: string
  验证码类型: string
  识别服务: string
  优先级: number
  等待时长: number
  重试上限: number
  降级方案: string
  命中触发条件: number
}

type TraceStep = {
  attempt: number
  stage: string
  service: string
  waited_seconds: number
  simulated_result: string
  detail: string
  counter_reset: boolean
}

type TrialResult = {
  trial_id: string
  scenario: string
  scenario_label: string
  candidates: Candidate[]
  skipped_strategies: Array<{ 策略编号: string; reason: string }>
  budget: {
    task_ref: string
    strategy_retry_cap: number
    task_retry_budget: number
    effective_retry_budget: number
    total_attempts_allowed: number
    warnings: string[]
  } | null
  trace: TraceStep[]
  outcome: string
  failure_record: Record<string, string | number> | null
  external_calls: number
}

type FailureRow = {
  id: number
  样本编号: string
  关联任务: string
  验证码类型: string
  对抗策略: string
  失败原因: string
  累计尝试次数: number
  生效重试预算: number
  降级链路: string
  处理状态: string
  来源: string
  试算编号?: string
}

type ConditionDraft = { operator: string; value: string }

type SampleDraft = {
  sample_id: string
  task_ref: string
  captcha_type: string
  http_status: string
  body_excerpt: string
  risk_level: string
}

const emptySample: SampleDraft = {
  sample_id: 'SAMPLE-TRIAL-01',
  task_ref: 'SPID-0002',
  captcha_type: '滑块',
  http_status: '403',
  body_excerpt: '请完成滑块验证',
  risk_level: '2',
}

function conditionsText(row: Row): string {
  const conditions = row['触发条件']
  if (!Array.isArray(conditions)) return String(conditions ?? '—')
  return conditions.map((item) => {
    const meta = conditionOperators.find((option) => option.code === item.operator)
    return `${meta?.label ?? item.operator} ${item.value}`
  }).join(' 且 ')
}

export default function AntibotView() {
  const [tab, setTab] = useState<'policies' | 'trial' | 'failures'>('policies')
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [executableOnly, setExecutableOnly] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const filterFields = useMemo(() => ['策略编号', '验证码类型', '识别服务'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters)
    if (executableOnly) query.set('executable_only', 'true')
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) throw new Error('对抗策略列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '反爬对抗列表读取失败')
    }
  }, [filters, executableOnly])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '反爬对抗动作未生效')
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '反爬对抗操作失败')
    }
  }

  return (
    <section className="page" data-module="antibot">
      <header className="page-head">
        <div>
          <h2>反爬对抗管理</h2>
          <p className="page-desc">
            维护结构化对抗策略与可执行判定，对同一挑战样本沙箱试算多条启用策略的优先级与淘汰理由；
            试算不调用真实外部验证码服务、不消耗生产任务次数。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate((value) => !value)}>
            {showCreate ? '收起登记表单' : '登记对抗策略'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出反爬对抗清单
          </button>
        </div>
      </header>

      <nav className="tab-bar">
        <button className={tab === 'policies' ? 'tab active' : 'tab'} type="button" onClick={() => setTab('policies')}>策略列表</button>
        <button className={tab === 'trial' ? 'tab active' : 'tab'} type="button" onClick={() => setTab('trial')}>挑战样本试算</button>
        <button className={tab === 'failures' ? 'tab active' : 'tab'} type="button" onClick={() => setTab('failures')}>失败记录处理</button>
      </nav>

      {notice && <p className="notice-text">{notice}</p>}
      {errorMessage && <p className="error-text">{errorMessage}</p>}

      {showCreate && <CreatePanel onDone={() => { setShowCreate(false); void reload() }} onError={setErrorMessage} />}

      {tab === 'policies' && (
        <>
          <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
            {filterFields.map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={filters[field] ?? ''}
                  placeholder={`按${field}检索`}
                  onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
            <label className="filter-item checkbox-item">
              <span>仅看可执行策略</span>
              <input type="checkbox" checked={executableOnly} onChange={(event) => setExecutableOnly(event.target.checked)} />
            </label>
            <button className="btn" type="submit">查询</button>
            <button className="btn ghost" type="button" onClick={() => { setFilters({}); setExecutableOnly(false) }}>重置条件</button>
          </form>

          <table className="data-table">
            <thead>
              <tr>
                <th>可执行判定</th>
                {columns.map((column) => <th key={column}>{column}</th>)}
                <th>可执行动作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={String(row.id)} className={row.executable ? '' : 'row-disabled'}>
                  <td>
                    <span className={row.executable ? 'badge badge-ok' : 'badge badge-block'}>
                      {row.executable ? '可执行' : '仅文字描述'}
                    </span>
                    {!row.executable && row.reasons && (
                      <ul className="reason-list">
                        {row.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                      </ul>
                    )}
                  </td>
                  {columns.map((column) => (
                    <td key={column}>{column === '触发条件' ? conditionsText(row) : (row[column] ?? '—')}</td>
                  ))}
                  <td className="row-actions">
                    {actions.map((action) => (
                      <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                        {action}
                      </button>
                    ))}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={columns.length + 2}>暂无符合条件的对抗策略</td>
                </tr>
              )}
            </tbody>
          </table>
          <footer className="page-foot">
            <span>共 {total} 条对抗策略记录（旧文字配置可读但不能作为可执行判定）</span>
          </footer>
        </>
      )}

      {tab === 'trial' && <TrialPanel onError={setErrorMessage} onNotice={setNotice} />}
      {tab === 'failures' && <FailurePanel onError={setErrorMessage} onNotice={setNotice} />}
    </section>
  )
}

// --------------------------------------------------------------------------- //
// 结构化登记面板
// --------------------------------------------------------------------------- //
function CreatePanel({ onDone, onError }: { onDone: () => void; onError: (message: string) => void }) {
  const [form, setForm] = useState({
    策略编号: '',
    验证码类型: captchaTypes[0],
    识别服务: recognitionServices[0],
    等待时长: '3',
    重试上限: '3',
    降级方案: fallbackPlans[0],
    优先级: '5',
  })
  const [conditions, setConditions] = useState<ConditionDraft[]>([{ operator: 'http_status', value: '403' }])
  const [feedback, setFeedback] = useState('')

  function patch(key: string, value: string) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  async function submit() {
    setFeedback('')
    const payload = {
      values: {
        ...form,
        等待时长: Number(form.等待时长),
        重试上限: Number(form.重试上限),
        优先级: Number(form.优先级),
        触发条件: conditions.map((item) => ({
          operator: item.operator,
          value: item.operator === 'http_status' || item.operator === 'risk_level_gte'
            ? Number(item.value)
            : item.value,
        })),
      },
    }
    try {
      const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify(payload) })
      const data = await response.json()
      if (!response.ok || data.ok === false) {
        setFeedback(data.message ?? '登记失败')
        return
      }
      onDone()
    } catch (error) {
      onError(error instanceof Error ? error.message : '登记请求失败')
    }
  }

  return (
    <div className="panel">
      <h3>登记结构化对抗策略</h3>
      <p className="page-desc">触发条件以算子+取值保存，是策略参与自动裁决与试算的前提；纯文字描述无法启用。</p>
      <div className="form-grid">
        <label className="filter-item"><span>策略编号</span><input value={form.策略编号} onChange={(e) => patch('策略编号', e.target.value)} placeholder="如 ANTI-2001" /></label>
        <label className="filter-item"><span>验证码类型</span>
          <select value={form.验证码类型} onChange={(e) => patch('验证码类型', e.target.value)}>{captchaTypes.map((v) => <option key={v}>{v}</option>)}</select>
        </label>
        <label className="filter-item"><span>识别服务</span>
          <select value={form.识别服务} onChange={(e) => patch('识别服务', e.target.value)}>{recognitionServices.map((v) => <option key={v}>{v}</option>)}</select>
        </label>
        <label className="filter-item"><span>等待时长（秒）</span><input type="number" min={0} value={form.等待时长} onChange={(e) => patch('等待时长', e.target.value)} /></label>
        <label className="filter-item"><span>重试上限（次）</span><input type="number" min={0} value={form.重试上限} onChange={(e) => patch('重试上限', e.target.value)} /></label>
        <label className="filter-item"><span>优先级（数值越大越优先）</span><input type="number" min={0} value={form.优先级} onChange={(e) => patch('优先级', e.target.value)} /></label>
        <label className="filter-item"><span>降级方案</span>
          <select value={form.降级方案} onChange={(e) => patch('降级方案', e.target.value)}>{fallbackPlans.map((v) => <option key={v}>{v}</option>)}</select>
        </label>
      </div>
      <div className="condition-editor">
        <div className="condition-head">
          <strong>触发条件（多条之间为“且”）</strong>
          <button className="btn" type="button" onClick={() => setConditions((prev) => [...prev, { operator: 'body_contains', value: '' }])}>新增条件</button>
        </div>
        {conditions.map((item, index) => (
          <div className="condition-row" key={index}>
            <select value={item.operator} onChange={(e) => setConditions((prev) => prev.map((row, i) => i === index ? { ...row, operator: e.target.value } : row))}>
              {conditionOperators.map((option) => <option key={option.code} value={option.code}>{option.label}</option>)}
            </select>
            <input value={item.value} placeholder="条件取值" onChange={(e) => setConditions((prev) => prev.map((row, i) => i === index ? { ...row, value: e.target.value } : row))} />
            <button className="link" type="button" onClick={() => setConditions((prev) => prev.filter((_, i) => i !== index))}>删除</button>
          </div>
        ))}
      </div>
      {feedback && <p className="error-text">{feedback}</p>}
      <div className="panel-actions">
        <button className="btn primary" type="button" onClick={() => void submit()}>保存为草稿</button>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 挑战样本试算面板
// --------------------------------------------------------------------------- //
function TrialPanel({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [scenarios, setScenarios] = useState<Array<{ code: string; label: string }>>([])
  const [scenario, setScenario] = useState('service_unavailable')
  const [sample, setSample] = useState<SampleDraft>(emptySample)
  const [result, setResult] = useState<TrialResult | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    void request(`${ENDPOINT}/scenarios`).then(async (response) => {
      if (response.ok) {
        const data = await response.json()
        setScenarios(data.items ?? [])
      }
    }).catch(() => undefined)
  }, [])

  function patchSample(key: keyof SampleDraft, value: string) {
    setSample((prev) => ({ ...prev, [key]: value }))
  }

  async function runTrial() {
    onError('')
    onNotice('')
    setLoading(true)
    try {
      const body = {
        scenario,
        sample: {
          sample_id: sample.sample_id,
          task_ref: sample.task_ref,
          captcha_type: sample.captcha_type,
          context: {
            http_status: Number(sample.http_status),
            body_excerpt: sample.body_excerpt,
            risk_level: Number(sample.risk_level),
          },
        },
      }
      const response = await request(`${ENDPOINT}/trial`, { method: 'POST', body: JSON.stringify(body) })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? '试算失败')
      setResult(data as TrialResult)
    } catch (error) {
      onError(error instanceof Error ? error.message : '试算请求失败')
    } finally {
      setLoading(false)
    }
  }

  async function materialize() {
    if (!result) return
    onError('')
    try {
      const response = await request(`${ENDPOINT}/failures`, {
        method: 'POST',
        body: JSON.stringify({ scenario, sample: {
          sample_id: sample.sample_id,
          task_ref: sample.task_ref,
          captcha_type: sample.captcha_type,
          context: {
            http_status: Number(sample.http_status),
            body_excerpt: sample.body_excerpt,
            risk_level: Number(sample.risk_level),
          },
        } }),
      })
      const data = await response.json()
      if (!response.ok || !data.ok) throw new Error(data.message ?? '失败记录落库失败')
      onNotice('失败记录已生成，可在“失败记录处理”页签办结')
    } catch (error) {
      onError(error instanceof Error ? error.message : '失败记录落库失败')
    }
  }

  return (
    <div className="panel">
      <h3>挑战样本试算（沙箱）</h3>
      <p className="page-desc">同一样本可反复提交，结果由输入内容决定且完全可重复；external_calls 恒为 0，不触碰真实外部服务与生产任务次数。</p>
      <div className="form-grid">
        <label className="filter-item"><span>样本编号</span><input value={sample.sample_id} onChange={(e) => patchSample('sample_id', e.target.value)} /></label>
        <label className="filter-item"><span>关联任务编号</span><input value={sample.task_ref} onChange={(e) => patchSample('task_ref', e.target.value)} /></label>
        <label className="filter-item"><span>验证码类型</span>
          <select value={sample.captcha_type} onChange={(e) => patchSample('captcha_type', e.target.value)}>{captchaTypes.map((v) => <option key={v}>{v}</option>)}</select>
        </label>
        <label className="filter-item"><span>HTTP 状态码</span><input type="number" value={sample.http_status} onChange={(e) => patchSample('http_status', e.target.value)} /></label>
        <label className="filter-item"><span>正文片段</span><input value={sample.body_excerpt} onChange={(e) => patchSample('body_excerpt', e.target.value)} /></label>
        <label className="filter-item"><span>风险等级</span><input type="number" value={sample.risk_level} onChange={(e) => patchSample('risk_level', e.target.value)} /></label>
        <label className="filter-item"><span>模拟场景</span>
          <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
            {scenarios.map((item) => <option key={item.code} value={item.code}>{item.label}</option>)}
          </select>
        </label>
      </div>
      <div className="panel-actions">
        <button className="btn primary" type="button" disabled={loading} onClick={() => void runTrial()}>{loading ? '试算中…' : '开始试算'}</button>
      </div>

      {result && (
        <div className="trial-result">
          <div className="result-head">
            <span className="badge badge-info">试算编号 {result.trial_id}</span>
            <span className={result.outcome === 'solved' ? 'badge badge-ok' : 'badge badge-block'}>
              结果：{result.outcome === 'solved' ? '识别成功' : result.outcome === 'budget_exhausted' ? '预算耗尽' : result.outcome === 'no_strategy' ? '无匹配策略' : '未解决'}
            </span>
            <span className="badge badge-info">外部调用 {result.external_calls} 次</span>
          </div>

          <h4>候选策略裁决（重叠条件下的优先级与淘汰理由）</h4>
          <table className="data-table">
            <thead>
              <tr><th>名次</th><th>裁决</th><th>策略编号</th><th>识别服务</th><th>优先级</th><th>命中条件数</th><th>理由</th></tr>
            </thead>
            <tbody>
              {result.candidates.map((candidate) => (
                <tr key={candidate.strategy_id} className={candidate.decision === '选中' ? 'row-winner' : ''}>
                  <td>{candidate.rank}</td>
                  <td><span className={candidate.decision === '选中' ? 'badge badge-ok' : 'badge badge-block'}>{candidate.decision}</span></td>
                  <td>{candidate.策略编号}</td>
                  <td>{candidate.识别服务}</td>
                  <td>{candidate.优先级}</td>
                  <td>{candidate.命中触发条件}</td>
                  <td>{candidate.reason}</td>
                </tr>
              ))}
              {result.candidates.length === 0 && <tr><td className="empty-state" colSpan={7}>没有启用且可执行的策略匹配本样本</td></tr>}
            </tbody>
          </table>

          {result.skipped_strategies.length > 0 && (
            <>
              <h4>未参与裁决的策略及原因</h4>
              <ul className="reason-list">
                {result.skipped_strategies.map((item) => <li key={item.策略编号}>{item.策略编号}：{item.reason}</li>)}
              </ul>
            </>
          )}

          {result.budget && (
            <>
              <h4>重试预算联动（取对抗策略上限与任务预算的较小值）</h4>
              <div className="budget-grid">
                <span>关联任务：{result.budget.task_ref}</span>
                <span>策略重试上限：{result.budget.strategy_retry_cap}</span>
                <span>任务重试预算：{result.budget.task_retry_budget}</span>
                <span className="budget-effective">生效预算：{result.budget.effective_retry_budget}（共允许 {result.budget.total_attempts_allowed} 次尝试）</span>
              </div>
              {result.budget.warnings.map((warning) => <p key={warning} className="notice-text">{warning}</p>)}

              <h4>逐次尝试轨迹（切换降级方案不重置计数）</h4>
              <table className="data-table">
                <thead>
                  <tr><th>累计尝试</th><th>阶段</th><th>识别服务</th><th>等待秒数</th><th>模拟结果</th><th>计数是否重置</th><th>说明</th></tr>
                </thead>
                <tbody>
                  {result.trace.map((step, index) => (
                    <tr key={index}>
                      <td>{step.attempt}</td>
                      <td>{step.stage}</td>
                      <td>{step.service}</td>
                      <td>{step.waited_seconds}</td>
                      <td>{step.simulated_result}</td>
                      <td>{step.counter_reset ? '是' : '否（保持累计）'}</td>
                      <td>{step.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {result.failure_record && (
            <div className="failure-preview">
              <h4>预算耗尽，生成可处理的失败记录（试算本身不落库）</h4>
              <ul className="reason-list">
                {Object.entries(result.failure_record).map(([key, value]) => <li key={key}>{key}：{String(value)}</li>)}
              </ul>
              <button className="btn primary" type="button" onClick={() => void materialize()}>确认落库为失败记录</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- //
// 失败记录处理面板
// --------------------------------------------------------------------------- //
function FailurePanel({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [records, setRecords] = useState<FailureRow[]>([])
  const [total, setTotal] = useState(0)

  const reload = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/failures?size=100`)
      if (!response.ok) throw new Error('失败记录读取失败')
      const data = await response.json()
      setRecords(data.items ?? [])
      setTotal(data.total ?? 0)
    } catch (error) {
      onError(error instanceof Error ? error.message : '失败记录读取失败')
    }
  }, [onError])

  useEffect(() => {
    void reload()
  }, [reload])

  async function resolve(record: FailureRow) {
    const note = window.prompt(`办结失败记录 ${record.样本编号}，请填写处理备注`, '已转人工处理')
    if (note === null) return
    onError('')
    try {
      const response = await request(`${ENDPOINT}/failures/${record.id}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ note }),
      })
      const data = await response.json()
      if (!response.ok || !data.ok) throw new Error(data.message ?? '办结失败')
      onNotice(data.message)
      await reload()
    } catch (error) {
      onError(error instanceof Error ? error.message : '办结失败')
    }
  }

  return (
    <div className="panel">
      <h3>失败记录处理</h3>
      <p className="page-desc">仅当等待与重试同时耗尽对抗策略上限与关联任务预算后才会产生，切换降级方案不会重置计数。</p>
      <table className="data-table">
        <thead>
          <tr>
            <th>样本编号</th><th>关联任务</th><th>验证码类型</th><th>对抗策略</th>
            <th>累计尝试</th><th>生效预算</th><th>降级链路</th><th>处理状态</th><th>来源</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {records.map((record) => (
            <tr key={record.id}>
              <td>{record.样本编号}</td>
              <td>{record.关联任务}</td>
              <td>{record.验证码类型}</td>
              <td>{record.对抗策略}</td>
              <td>{record.累计尝试次数}</td>
              <td>{record.生效重试预算}</td>
              <td>{record.降级链路}</td>
              <td><span className={record.处理状态 === '已处理' ? 'badge badge-ok' : 'badge badge-block'}>{record.处理状态}</span></td>
              <td>{record.来源}</td>
              <td>
                {record.处理状态 !== '已处理'
                  ? <button className="link" type="button" onClick={() => void resolve(record)}>办结</button>
                  : '—'}
              </td>
            </tr>
          ))}
          {records.length === 0 && <tr><td className="empty-state" colSpan={10}>暂无失败记录</td></tr>}
        </tbody>
      </table>
      <footer className="page-foot"><span>共 {total} 条失败记录</span></footer>
    </div>
  )
}
