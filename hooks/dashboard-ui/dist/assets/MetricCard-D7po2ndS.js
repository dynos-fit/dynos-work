import{c as s,j as n,m as r}from"./index-CvA20u4o.js";/**
 * @license lucide-react v0.487.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const x=[["path",{d:"M5 12h14",key:"1ays0h"}]],i=s("minus",x);/**
 * @license lucide-react v0.487.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const m=[["polyline",{points:"22 17 13.5 8.5 8.5 13.5 2 7",key:"1r2t7k"}],["polyline",{points:"16 17 22 17 22 11",key:"11uiuu"}]],u=s("trending-down",m);/**
 * @license lucide-react v0.487.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const y=[["polyline",{points:"22 7 13.5 15.5 8.5 10.5 2 17",key:"126l90"}],["polyline",{points:"16 7 22 7 22 13",key:"kwv8wd"}]],h=s("trending-up",y);function g({label:o,value:a,trend:e,trendLabel:c="vs last period",icon:l,delay:d=0}){const t=e==null?"text-[#7A776E]":e>0?"text-[#BDF000]":e<0?"text-[#FF3B3B]":"text-[#7A776E]",p=e==null?i:e>0?h:e<0?u:i;return n.jsxs(r.div,{className:"border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5 card-hover-glow",initial:{opacity:0,y:12},animate:{opacity:1,y:0},transition:{delay:d,duration:.28,ease:"easeOut"},children:[n.jsxs("div",{className:"flex items-center gap-1.5 mb-3",children:[l,n.jsx("span",{className:"text-[10px] text-[#7A776E] tracking-[0.12em] uppercase",children:o})]}),n.jsx("div",{className:"text-[28px] font-bold text-[#F0F0E8] leading-none",style:{fontFamily:"'JetBrains Mono', monospace"},children:a}),e!=null&&n.jsxs("div",{className:"flex items-center gap-1.5 mt-2",children:[n.jsx(p,{className:`w-3 h-3 ${t}`}),n.jsxs("span",{className:`text-[11px] font-medium ${t}`,children:[e>0?"+":"",e.toFixed(1),"%"]}),n.jsx("span",{className:"text-[10px] text-[#5A574E]",children:c})]})]})}export{g as M,h as T,u as a};
