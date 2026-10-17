import { useRef, type CSSProperties, type PointerEvent, type ReactNode } from 'react';
type Crop = { file: string; size: [number,number]; rect: [number,number,number,number] };
const crop = (file:string, rect:Crop['rect'], size:Crop['size']=[852,1847]):Crop => ({file,rect,size});
export const artwork:Record<string,Crop>={
 sea:crop('01-collection-home.png',[54,345,584,351]),
 lighthouse:crop('02-category-collection.png',[41,440,370,522],[852,1846]),
 lighthouseSunset:crop('04-friend-shares.png',[41,458,744,352]),
 train:crop('02-category-collection.png',[443,440,370,522],[852,1846]),
 spring:crop('02-category-collection.png',[443,1052,370,293],[852,1846]),
 ticket:crop('02-category-collection.png',[41,1067,370,171],[852,1846]),
 cinema:crop('02-category-collection.png',[41,1339,370,242],[852,1846]),
 film:crop('02-category-collection.png',[443,1431,370,179],[852,1846]),
 cafe:crop('01-collection-home.png',[42,1433,212,209]),
 pasta:crop('01-collection-home.png',[276,1433,207,209]),
 bread:crop('01-collection-home.png',[505,1433,210,209]),
 cat:crop('01-collection-home.png',[737,1433,113,209]),
 city:crop('03-explore.png',[49,396,663,368],[852,1846]),
 jazz:crop('04-friend-shares.png',[446,1008,371,224]),
 exhibition:crop('03-explore.png',[440,972,369,270],[852,1846]),
 outdoor:crop('03-explore.png',[48,972,369,270],[852,1846]),
 avatar:crop('05-profile.png',[38,149,158,159],[850,1850]),
 monthOctober:crop('05-profile.png',[38,946,437,354],[850,1850]),
 monthSeptember:crop('05-profile.png',[500,946,318,354],[850,1850]),
};
// The supplied raster photos/posters remain intact. The viewport shows only the
// measured photograph region; all surrounding application UI is real markup.
export function Art({name,src,alt,className='',ratio}:{name:string;src?:string;alt:string;className?:string;ratio?:number}){
 const a=artwork[name]||artwork.sea;const [x,y,w,h]=a.rect;const r=ratio||w/h;
 const scale=Math.max(1,w/h/r);const height=w/r;
 const style:CSSProperties={width:`${a.size[0]/w*scale*100}%`,left:`${(-x*scale-(w*scale-w)/2)/w*100}%`,top:`${(-y*scale-(h*scale-height)/2)/height*100}%`};
 return <span className={`artwork ${className}`} style={src&&!ratio?undefined:{aspectRatio:r}}>{src?<img className="uploaded-image" style={!ratio?{position:'relative',width:'100%',height:'auto'}:undefined} src={src} alt={alt} draggable={false}/>:<img src={`/assets/reference/${a.file}`} style={style} alt={alt} draggable={false} loading="lazy"/>}</span>;
}
export function Tilt({children,className=''}:{children:ReactNode;className?:string}){
 const el=useRef<HTMLDivElement>(null);
 const move=(e:PointerEvent<HTMLDivElement>)=>{if(e.pointerType!=='mouse'||window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;const r=e.currentTarget.getBoundingClientRect();const x=(e.clientX-r.left)/r.width-.5;const y=(e.clientY-r.top)/r.height-.5;if(el.current)el.current.style.transform=`perspective(1000px) rotateX(${-y*7}deg) rotateY(${x*7}deg)`;};
 return <div ref={el} className={`tilt ${className}`} onPointerMove={move} onPointerLeave={()=>{if(el.current)el.current.style.transform='';}}>{children}</div>;
}
