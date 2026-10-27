// Memory-only DOM retention. Call render with current data so receipts never come from a stale cache.
export function createPageCache(selector, identity=()=>'', max=16) {
  const pages=new Map();let active='',owner=identity();
  const root=()=>document.querySelector(selector);
  const controls=node=>[...node.querySelectorAll('input,textarea,select')];
  function capture(node) {
    const fields=controls(node).map((field,index)=>({key:field.id||field.name||String(index),value:field.value,checked:field.checked}));
    const focused=controls(node).indexOf(document.activeElement);
    const selection=focused<0?null:[document.activeElement.selectionStart,document.activeElement.selectionEnd];
    return {fields,focused,selection,details:[...node.querySelectorAll('details')].map(n=>n.open),scroll:[window.scrollX,window.scrollY],
      scrollers:[...node.querySelectorAll('*')].map((n,index)=>[index,n.scrollLeft,n.scrollTop]).filter(([,x,y])=>x||y)};
  }
  function restore(node,saved,fields=false) {
    if(!saved)return;
    if(fields){controls(node).forEach((field,index)=>{const value=saved.fields.find(f=>f.key===(field.id||field.name||String(index)));if(value&&field.type!=='file'){field.value=value.value;field.checked=value.checked;}});node.querySelectorAll('details').forEach((n,i)=>{n.open=saved.details[i]??n.open;});}
    for(const [index,x,y] of saved.scrollers)node.querySelectorAll('*')[index]?.scrollTo(x,y);
    if(fields&&saved.focused>=0){const field=controls(node)[saved.focused];field?.focus({preventScroll:true});if(field&&saved.selection?.[0]!=null&&field.setSelectionRange)field.setSelectionRange(...saved.selection);}
    window.scrollTo(...saved.scroll);
  }
  function render(key,html) {
    const node=root();if(!node)return false;
    if(owner!==identity()){pages.clear();active='';owner=identity();}
    const switching=active!==key;
    if(switching&&active&&pages.has(active)){
      const previous=pages.get(active);previous.saved=capture(node);previous.fragment=document.createDocumentFragment();previous.fragment.append(...node.childNodes);
    }
    let entry=pages.get(key),updated=false;
    if(switching){node.replaceChildren();if(entry?.fragment)node.append(entry.fragment);active=key;}
    if(!entry||entry.html!==html){
      const saved=entry?(switching?entry.saved:capture(node)):null;
      node.innerHTML=html;updated=true;
      entry={html,saved:saved||entry?.saved};pages.set(key,entry);
      restore(node,entry.saved,true);
    }
    if(switching)restore(node,entry.saved);
    // LRU is bounded by known workspace pages, never by search/query combinations.
    pages.delete(key);pages.set(key,entry);
    while(pages.size>max)pages.delete(pages.keys().next().value);
    return updated;
  }
  return {render,clear(){pages.clear();active='';}};
}
