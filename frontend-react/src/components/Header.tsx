import React from 'react';

const Header: React.FC = () => (
  <header className="flex flex-col items-center text-center">
    <h1 className="text-3xl font-black text-slate-900 mb-2 tracking-tight">Fin-Us Agent Console</h1>
    <p className="text-sm font-bold text-slate-400">FastAPI 백엔드 연동 테스트 대시보드</p>
  </header>
);

export default Header;
