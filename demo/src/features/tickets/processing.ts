import type { ProcessingStatus } from '../../data';

export const processingLabels: Record<ProcessingStatus, string> = {
  not_started: '尚未处理', queued: '等待处理', ocr: '正在识别票面', poster: '正在查找海报',
  ready: '处理完成', no_match: '未找到可靠海报', failed: '处理失败',
};
export const processingActive = (status: ProcessingStatus) => ['queued', 'ocr', 'poster'].includes(status);
export const sourceLabels = { user: '手动填写', ocr: '识别结果', migration: '迁移保留', empty: '尚未填写' };

export function processingDescription(status: ProcessingStatus) {
  const descriptions: Record<ProcessingStatus, string> = {
    not_started: '原票已保留，尚未开始自动识别。',
    queued: '处理请求已登记，正在等待服务端处理。可以继续浏览。',
    ocr: '服务端正在识别电影信息，已手动填写的字段优先保留。',
    poster: '正在检索、下载并保存可信海报；新封面发布前保留当前图片。',
    ready: '识别流程已结束，海报已发布。字段仍可手动纠正。',
    no_match: '没有找到可靠海报，使用原票作为封面。可以纠正片名后重试。',
    failed: '处理失败，原票照片仍被保留，可以稍后重试。',
  };
  return descriptions[status];
}
