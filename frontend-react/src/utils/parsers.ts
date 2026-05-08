import { TrendItem } from '../types';

export const parseTrendData = (trendStr: string | null): TrendItem[] => {
  if (!trendStr) return [];
  const lines = trendStr.split('\n').filter(l => l.includes('|'));
  return lines.map(line => {
    const parts = line.split('|').map(p => p.trim());
    const date = parts[0]?.split(' ')[0] || '';
    const closeStr = parts[1]?.replace('종가: ', '').replace(/,/g, '').replace(/\s+/g, '') || '0';
    
    const changeFull = parts[2]?.replace('변동: ', '').trim() || '';
    const isUp = changeFull.includes('상승');
    const cleanChange = changeFull.replace('상승', '').replace('하락', '').trim();
    const changeVal = cleanChange.split(' (')[0].trim();
    const changePct = changeFull.includes('(') ? changeFull.split('(')[1].replace(')', '') : '0%';
    
    const foreStr = parts[3]?.replace('외인: ', '').replace(/,/g, '').replace(/\s+/g, '') || '0';
    const instStr = parts[4]?.replace('기관: ', '').replace(/,/g, '').replace(/\s+/g, '') || '0';
    const volStr = parts[5]?.replace('거래량: ', '').replace(/,/g, '').replace(/\s+/g, '') || '0';
    
    const result: TrendItem = {
      date: date.substring(5),
      price: parseInt(closeStr),
      changeVal,
      changePct,
      isUp,
      foreigner: parseInt(foreStr) || 0,
      institution: parseInt(instStr) || 0,
      volume: parseInt(volStr) || 0
    };
    return result;
  }).reverse();
};
