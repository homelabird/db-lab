import { useId } from 'react';

function Artwork({ identity, keyId }) {
  switch (identity) {
    case 'keyboard': return <><ellipse cx="150" cy="228" rx="116" ry="14" fill="#d5d5cd"/><g transform="translate(22 83) rotate(-8 128 62)"><rect width="256" height="126" rx="13" fill="#d4d4cb"/><rect y="-6" width="256" height="124" rx="13" fill="#faf9f2"/><path d="M17 13h222v69H17Z" fill={`url(#${keyId})`}/><rect x="61" y="89" width="125" height="14" rx="3" fill="#e1e1d7"/><rect x="17" y="89" width="34" height="14" rx="3" fill="#e1e1d7"/><rect x="196" y="89" width="43" height="14" rx="3" fill="#86937c"/></g></>;
    case 'lamp': return <><ellipse cx="150" cy="258" rx="78" ry="12" fill="#cdd3c3"/><ellipse cx="150" cy="246" rx="66" ry="12" fill="#66775b"/><path d="M144 114h12v130h-12Z" fill="#7c8c6e"/><path d="M150 32C96 32 60 83 60 123h180c0-40-36-91-90-91Z" fill="#7c8c6e"/><ellipse cx="150" cy="123" rx="90" ry="12" fill="#5d6d52"/><ellipse cx="150" cy="124" rx="70" ry="6" fill="#eee4c6"/></>;
    case 'mug': return <><ellipse cx="147" cy="240" rx="85" ry="13" fill="#d8c7b1"/><path d="M198 109h18c61 0 61 83 0 83h-21" fill="none" stroke="#faf5ea" strokeWidth="18"/><path d="M65 95h139l-7 110c-3 43-120 43-125 0Z" fill="#faf5ea"/><ellipse cx="135" cy="95" rx="70" ry="19" fill="#e0d2be"/><ellipse cx="135" cy="97" rx="55" ry="12" fill="#94765c"/><path d="M103 132v63" stroke="#fffdf8" strokeWidth="7" strokeLinecap="round"/></>;
    case 'headphones': return <><ellipse cx="150" cy="255" rx="86" ry="12" fill="#d0ceca"/><path d="M65 176v-54a85 85 0 0 1 170 0v54" fill="none" stroke="#464942" strokeWidth="24"/><path d="M65 127v-5a85 85 0 0 1 170 0v5" fill="none" stroke="#696e61" strokeWidth="16"/><g transform="rotate(-10 73 185)"><rect x="47" y="140" width="47" height="101" rx="21" fill="#4b5145"/><rect x="77" y="151" width="22" height="81" rx="10" fill="#282d25"/></g><g transform="rotate(10 225 185)"><rect x="205" y="140" width="47" height="101" rx="21" fill="#4b5145"/><rect x="200" y="151" width="22" height="81" rx="10" fill="#282d25"/></g></>;
    default: return null;
  }
}

export function ProductArt({ product }) {
  const keyId = 'keys-' + useId();
  return <svg viewBox="0 0 300 300" aria-hidden="true">
    <defs><pattern id={keyId} width="22" height="23" patternUnits="userSpaceOnUse"><rect x="2" y="2" width="18" height="18" rx="3" fill="#e6e6dd" /></pattern></defs>
    <Artwork identity={product.id} keyId={keyId} />
  </svg>;
}
