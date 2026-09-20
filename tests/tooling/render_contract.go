// Limited Go-template contract renderer for this chart, NOT Helm.
// Validates template execution + YAML contracts offline. Does NOT validate Helm,
// Kubernetes API admission, lookups, image behavior, networking, or a real cluster.
package main
import("bytes";"crypto/sha256";"encoding/json";"fmt";"os";"path/filepath";"reflect";"strconv";"strings";"text/template")
type Files struct{ Root string }
func(f Files)Get(n string)string{b,e:=os.ReadFile(filepath.Join(f.Root,n));if e!=nil{panic(e)};return string(b)}
func empty(v any)bool{if v==nil{return true};r:=reflect.ValueOf(v);switch r.Kind(){case reflect.String,reflect.Array,reflect.Slice,reflect.Map:return r.Len()==0;case reflect.Bool:return !r.Bool();case reflect.Int,reflect.Int64:return r.Int()==0;case reflect.Float64:return r.Float()==0};return false}
func main(){if len(os.Args)!=3{panic("usage: render_contract CHART CONTEXT_JSON")};dir:=os.Args[1];b,e:=os.ReadFile(os.Args[2]);if e!=nil{panic(e)};ctx:=map[string]any{};if e=json.Unmarshal(b,&ctx);e!=nil{panic(e)};ctx["Files"]=Files{dir};var t *template.Template
 indent:=func(n int,s string)string{return strings.Repeat(" ",n)+strings.ReplaceAll(s,"\n","\n"+strings.Repeat(" ",n))}
 funcs:=template.FuncMap{
 "sha256sum":func(s string)string{return fmt.Sprintf("%x",sha256.Sum256([]byte(s)))},
 "include":func(name string,data any)(string,error){var b bytes.Buffer;e:=t.ExecuteTemplate(&b,name,data);return b.String(),e},
 "dict":func(args ...any)(map[string]any,error){m:=map[string]any{};if len(args)%2!=0{return nil,fmt.Errorf("odd dict args")};for i:=0;i<len(args);i+=2{m[args[i].(string)]=args[i+1]};return m,nil},
 "default":func(d,v any)any{if empty(v){return d};return v},
 "trunc":func(n int,s string)string{if len(s)>n{return s[:n]};return s},
 "trimSuffix":func(suffix,s string)string{return strings.TrimSuffix(s,suffix)},
 "replace":func(old,new,s string)string{return strings.ReplaceAll(s,old,new)},
 "quote":func(v any)string{return strconv.Quote(fmt.Sprint(v))},
 "indent":indent,"nindent":func(n int,s string)string{return "\n"+indent(n,s)},
 "toYaml":func(v any)(string,error){b,e:=json.Marshal(v);return string(b),e},
 "int":func(v any)int{switch n:=v.(type){case float64:return int(n);case int:return n;case string:i,_:=strconv.Atoi(n);return i};return 0},
 "ternary":func(a,b any,c bool)any{if c{return a};return b},
 "fail":func(msg string)(string,error){return "",fmt.Errorf("%s",msg)},
 }
 t=template.New("root").Funcs(funcs).Option("missingkey=zero")
 paths,_:=filepath.Glob(filepath.Join(dir,"templates","*"));for _,p:=range paths{content,err:=os.ReadFile(p);if err!=nil{panic(err)};if _,err=t.New(filepath.Base(p)).Parse(string(content));err!=nil{panic(err)}}
 for _,p:=range paths{if strings.HasPrefix(filepath.Base(p),"_") || !strings.HasSuffix(p,".yaml"){continue};var out bytes.Buffer;if err:=t.ExecuteTemplate(&out,filepath.Base(p),ctx);err!=nil{panic(err)};if strings.TrimSpace(out.String())!=""{fmt.Printf("---\n# Source: %s\n%s\n",filepath.Base(p),out.String())}}
}
