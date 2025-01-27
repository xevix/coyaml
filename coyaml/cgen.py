import textwrap
from collections import defaultdict

from . import load, core
from .util import builtin_conversions, parse_int, parse_float, nested
from .cutil import varname, string, typename, cbool
from .cast import *

cmdline_template = """\
Usage:
    {m.program_name} [options]

Description:
{description}

Options:
  -h, --help        Print this help
  -c, --config FILE Name of configuration file
  --debug-config    Print debugging information while parsing configuration file
  --config-vars     Enable variables in configuration file (by default)
  --config-no-vars  Disable variables in configuration file
  -D,--config-var NAME=VALUE
                    Set value of configuration variable NAME to value VALUE
  -P,--print-config Print read configuration. Including command-line overrides
                    Double this flag (`-PP`) to include parameter descriptions
  -C,--check-config Only check configuration file and exit
{options}
"""

scalar_types = {
    load.Int: lambda val: Int(parse_int(val)),
    load.UInt: lambda val: Int(parse_int(val)),
    load.Float: lambda val: Float(parse_float(val)),
    load.VoidPtr: lambda val: NULL,
    }
string_types = {
    load.String,
    load.File,
    load.Dir,
    }

def mem2dotname(mem):
    if isinstance(mem, Dot):
        return Dot(mem2dotname(mem.source), mem.name)
    elif isinstance(mem, Member):
        return mem.name
    elif isinstance(mem, Expression):
        return Expression(mem2dotname(mem.expr))
    else:
        raise NotImplementedError(mem)

def bitmask(*args):
    res = 0
    for i, v in enumerate(args):
        if v:
            res |= 1 << i
    return res

class StructInfo(object):
    def __init__(self, name, inheritance=False):
        self.name = name
        self.flagcount = 1 if inheritance else 0
        self.a_ptr = Typename(name + ' *')
        self.a_name = Ident(name)
        self.inheritance = inheritance

    def nextflag(self):
        if self.inheritance:
            val = self.flagcount
            self.flagcount += 1
            return val
        else:
            return 0

class ArrayEl(object):

    def __init__(self, name):
        self.name = name
        self.a_ptr = Typename(name + ' *')
        self.a_name = Ident(name)

    def nextflag(self):
        return 0

class MappingEl(object):

    def __init__(self, name):
        self.name = name
        self.a_ptr = Typename(name + ' *')
        self.a_name = Ident(name)

    def nextflag(self):
        return 0

class GenCCode(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.prefix = cfg.name

    def _vars(self, ast, decl=False):
        items = ('group', 'string', 'file', 'dir', 'int', 'uint', 'float',
            'custom', 'mapping', 'array', 'bool')
        if decl:
            for i in items:
                ast(Var('coyaml_'+i+'_t',
                    self.prefix+'_'+i+'_vars',
                    static=True, array=(None,)))
        else:
            self.states = {}
            for i in items:
                res = ast(VarAssign('coyaml_'+i+'_t',
                    self.prefix+'_'+i+'_vars', Arr(ast.block()),
                    static=True, array=(None,)))
                self.states[i] = res
            return self.states.values()

    def _clear_unused_vars(self, transitions, vars):
        to_remove = set()
        for name, zone in self.states.items():
            if not zone.content:
                to_remove.add('_{}_'.format(name))
        for item in transitions.content[:]:
            if isinstance(item, Var):
                for name in to_remove:
                    if name in item.name.value:
                        transitions.content.remove(item)
                        break
        for item in vars.content[:]:
            if isinstance(item, Var):
                for name in to_remove:
                    if name in item.name.value:
                        vars.content.remove(item)
                        break

    def _next_default_fun(self):
        self.default_fun_no += 1
        return self.default_fun_no

    def make(self, ast):
        self.default_fun_no = 0
        ast(CommentBlock(
            'THIS IS AUTOGENERATED FILE',
            'DO NOT EDIT!!!',
            ))
        ast(Define('_GNU_SOURCE'))
        ast(StdInclude('coyaml_src.h'))
        ast(StdInclude('stdlib.h'))
        ast(StdInclude('stdio.h'))
        ast(StdInclude('errno.h'))
        ast(StdInclude('strings.h'))
        ast(Include(self.cfg.targetname+'.h'))
        ast(VSpace())
        self.lasttran = 0
        self._vars(ast.zone('transitions'), decl=True)
        ast(VSpace())
        ast.zone('usertypes')
        ast(VSpace())
        vars = ast.zone('vars')
        ast(VSpace())
        cli = ast.zone('cli')
        ast(VSpace())
        with nested(*self._vars(vars, decl=False)):
            self.visit_hier(ast)

        self.make_options(cli)
        self.make_environ(vars)

        mainstr = Typename(self.prefix+'_main_t')
        mainptr = Typename(self.prefix+'_main_t *')
        with ast(Function(mainptr, self.prefix+'_init', [
            Param(mainptr, Ident('ptr')) ], ast.block())) as init:
            init(VarAssign(mainptr, 'res', Ident('ptr')))
            with init(If(Not(Ident('res')), ast.block())) as if_:
                if_(Statement(Assign('res', Coerce(mainptr,
                    Call('malloc', [ Call('sizeof', [ mainstr ])])))))
            with init(If(Not(Ident('res')), ast.block())) as if_:
                if_(Return(Ident('NULL')))
            init(Statement(Call('bzero', [ Ident('res'),
                Call('sizeof', [ mainstr ]) ])))
            with init(If(Not(Ident('ptr')), init.block())) as if_:
                if_(Statement(Assign(Dot(Member(Ident('res'), 'head'),
                    'free_object'), Ident('TRUE'))))
            init(Statement(Call('obstack_init', [
                Ref(Dot(Member(Ident('res'), 'head'), 'pieces'))])))
            init(Statement(Call(self.prefix + '_defaults', [ Ident('res') ])))
            init(Return(Ident('res')))

        with ast(Function(Typename('coyaml_context_t *'),
            self.prefix+'_context', [
            Param(Typename('coyaml_context_t *'), 'inp'),
            Param(Typename(self.prefix+'_main_t *'), 'tinp'),
            ], ast.block())) as ctx:
            _ctx = Ident('ctx')
            ctx(VarAssign('coyaml_context_t *', _ctx,
                Call('coyaml_context_init', [ Ident('inp') ])))
            with ctx(If(Not(_ctx), ctx.block())) as if_:
                if_(Return(NULL))
            ctx(Statement(Assign(Member(_ctx, 'target'), Coerce(
                Typename('coyaml_head_t *'),
                Call(self.prefix + '_init', [ Ident('tinp') ])))))
            with ctx(If(Not(Member(_ctx, 'target')), ctx.block())) as if_:
                if_(Statement(Call('coyaml_context_free', [ _ctx ])))
                if_(Return(NULL))
            ctx(Statement(Assign(Member(_ctx, 'program_name'),
                String(self.cfg.meta.program_name))))

            fn = Member(_ctx, 'root_filename')
            if hasattr(self.cfg.meta, 'environ_filename'):
                ctx(Statement(Assign(fn,
                    Call('getenv', [String(self.cfg.meta.environ_filename)]))))
                with ctx(If(Not(fn), ctx.block())) as if_:
                    if_(Statement(Assign(fn,
                        String(self.cfg.meta.default_config))))
            else:
                ctx(Statement(Assign(fn,
                    String(self.cfg.meta.default_config))))

            ctx(Statement(Assign(Member(_ctx, 'cmdline'),
                Ref(self.prefix + '_cmdline'))))
            ctx(Statement(Assign(Member(_ctx, 'env_vars'),
                Ident(self.prefix + '_env_vars'))))
            ctx(Statement(Assign(Member(_ctx, 'root_group'), Ref(Subscript(
                Ident(self.prefix+'_group_vars'),
                Int(len(self.states['group'].content)-1))))))
            ctx(Return(_ctx))

        with ast(Function(Void(), self.prefix+'_free', [
            Param(mainptr, Ident('ptr')) ], ast.block())) as free:
            free(Statement(Call('obstack_free', [
                Ref(Dot(Member(Ident('ptr'), 'head'), 'pieces')),
                Ident('NULL') ])))
            with free(If(Dot(Member(Ident('ptr'), 'head'), 'free_object'),
                ast.block())) as if_:
                if_(Statement(Call('free', [ Ident('ptr') ])))

        with ast(Function(Typename('bool'), self.prefix+'_readfile', [
            Param('coyaml_context_t *', 'ctx'),
            ], ast.block())) as fun:
            fun(Return(Call('coyaml_readfile', [Ident('ctx')] )))

        errcheck = If(Or(
            Gt(Ident('errno'), Ident('ECOYAML_MAX')),
            Lt(Ident('errno'), Ident('ECOYAML_MIN'))), ast.block())

        with ast(Function(mainptr, self.prefix+'_load', [ Param(mainptr, 'ptr'),
            Param('int','argc'), Param('char**','argv') ], ast.block())) as fun:
            fun(Var('coyaml_context_t', 'ctx'))
            with fun(If(Lt(Call(self.prefix+'_context', [
                Ref(Ident('ctx')), Ident('ptr') ]), Int(0)),
                fun.block())) as if_:
                if_(Statement(Call('perror', [
                    Subscript(Ident('argv'), Int(0)) ])))
                if_(Statement(Call('exit', [ Int(1) ])))
            fun(Statement(Call('coyaml_cli_prepare_or_exit', [
                Ref(Ident('ctx')),
                Ident('argc'), Ident('argv')])))
            fun(Statement(Call('coyaml_readfile_or_exit',
                [ Ref(Ident('ctx')) ])))
            fun(Statement(Call('coyaml_env_parse_or_exit', [
                Ref(Ident('ctx')) ])))
            fun(Statement(Call('coyaml_cli_parse_or_exit', [ Ref(Ident('ctx')),
                Ident('argc'), Ident('argv') ])))
            fun(Statement(Call('coyaml_context_free', [ Ref(Ident('ctx')) ])))
            fun(Return(Coerce(mainptr, Dot(Ident('ctx'), Ident('target')))))

        self._clear_unused_vars(ast.zone('transitions'), ast.zone('vars'))

    def make_options(self, ast):
        optval = 1000
        visited = set()
        targets = []
        for opt in self.cfg.commandline:
            if not hasattr(opt.target, 'options'):
                opt.target.options = defaultdict(list)
                targets.append(opt.target)
            opt.target.options[opt.__class__].append(opt)
            key = id(opt.target), opt.__class__
            if key in visited:
                continue
            opt.index = optval
            optval += 1
        ast(Func('int', self.prefix+'_print', [
                Param('FILE *', 'out'),
                Param(self.prefix+'_main_t *', 'cfg'),
                Param('coyaml_print_enum', 'mode'),
                ]))
        with ast(VarAssign('struct option',
                self.prefix+'_getopt_ar', Arr(ast.block()),
                static=True, array=(None,))) as cmd,\
            ast(VarAssign('coyaml_option_t',
                self.prefix+'_options', Arr(ast.block()),
                static=True, array=(None,))) as copt:
            cmd(StrValue(name=String('help'), val=Int(500),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('config'), val=Int(501),
                flag='NULL', has_arg='TRUE')),
            cmd(StrValue(name=String('debug-config'), val=Int(502),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('config-vars'), val=Int(503),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('config-no-vars'), val=Int(504),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('config-var'), val=Int(505),
                flag='NULL', has_arg='TRUE')),
            cmd(StrValue(name=String('print-config'), val=Int(600),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('check-config'), val=Int(601),
                flag='NULL', has_arg='FALSE')),
            optstr = "hc:D:PC"
            optidx = [500, 501, -1, 505, -1, 600, 601]
            if not getattr(self.cfg.meta, 'mixed_arguments', True):
                optstr = "+" + optstr
                optidx.insert(0, -1)
            for opt in self.cfg.commandline:
                has_arg = opt.__class__ == core.Option
                if opt.char:
                    optidx.append(1000+len(copt.content))
                    optstr += opt.char
                    if has_arg:
                        optstr += ':'
                        optidx.append(-1)
                if opt.name:
                    cmd(StrValue(name=String(opt.name), val=Int(opt.index),
                        flag='NULL',
                        has_arg='TRUE' if has_arg else 'FALSE'))
                if isinstance(opt, core.IncrOption):
                    opt_fun = opt.target.prop_func + '_incr_o'
                elif isinstance(opt, core.DecrOption):
                    opt_fun = opt.target.prop_func + '_decr_o'
                elif isinstance(opt, core.EnableOption):
                    opt_fun = opt.target.prop_func + '_enable_o'
                elif isinstance(opt, core.DisableOption):
                    opt_fun = opt.target.prop_func + '_disable_o'
                elif isinstance(opt, core.Option):
                    opt_fun = opt.target.prop_func+'_o'
                else:
                    raise NotImplementedError(opt)
                copt(StrValue(
                    callback=Coerce('coyaml_option_fun', opt_fun),
                    prop=Coerce('coyaml_placeholder_t *', opt.target.prop_ref),
                    ))
            cmd(StrValue(name=NULL, val=Int(0), flag='NULL', has_arg='FALSE')),
        ast(VarAssign('int', self.prefix+'_optidx',
            Arr(list(map(Int, optidx))), static=True, array=(None,)))
        stroptions = []
        for target in targets:
            for typ in (core.Option,
                core.IncrOption, core.DecrOption,
                core.EnableOption, core.DisableOption,
                ):
                opt = ', '.join(o.param for o in target.options[typ])
                if not opt:
                    continue
                if typ != core.Option and target.options[core.Option]:
                    if typ == core.IncrOption:
                        description = 'Increment aformentioned value'
                    elif typ == core.DecrOption:
                        description = 'Decrement aformentioned value'
                    elif typ == core.EnableOption:
                        description = 'Enable aformentioned option'
                    elif typ == core.DisableOption:
                        description = 'Disable aformentioned option'
                else:
                    description = target.description
                if len(opt) <= 17:
                    opt = '  {:17s} '.format(opt)
                    stroptions.extend(textwrap.wrap(description,
                        width=80, initial_indent=opt, subsequent_indent=' '*20))
                else:
                    stroptions.append('  '+opt)
                    stroptions.extend(textwrap.wrap(description,
                        width=80, initial_indent=' '*20,
                        subsequent_indent=' '*20))
        descr = cmdline_template.format(m=self.cfg.meta,
            description='\n'.join(textwrap.wrap(self.cfg.meta.description,
                 width=80, initial_indent='    ', subsequent_indent='    ')),
            options='\n'.join(stroptions),
            )
        usage = "Usage: {m.program_name} [options]\n".format(m=self.cfg.meta)
        ast(VarAssign('coyaml_cmdline_t', self.prefix+'_cmdline',
            StrValue(
                optstr=String(optstr),
                optidx=self.prefix+'_optidx',
                usage=String(usage),
                full_description=String(descr),
                has_arguments=Ident('TRUE')
                    if getattr(self.cfg.meta, 'has_arguments', False)
                    else Ident('FALSE'),
                options=self.prefix+'_getopt_ar',
                coyaml_options=self.prefix+'_options',
                print_callback=Coerce('coyaml_print_fun',
                    Ref(self.prefix+'_print')),
            )))

    def make_environ(self, ast):
        ast(VarAssign('coyaml_env_var_t', self.prefix+'_env_vars', Arr([
            StrValue(
                name=String(ev.name),
                prop=Coerce('coyaml_placeholder_t *', ev.target.prop_ref),
                callback=Coerce('coyaml_option_fun', ev.target.prop_func+'_o'),
            ) for ev in self.cfg.environ]
            + [StrValue(name=NULL)]),
            array=(None,)))

    def visit_hier(self, ast):
        # Visits hierarchy to set appropriate structures and member
        # names for `offsetof()` in `baseoffset`
        ast(VSpace())
        self._mk_defaultsfun(self.prefix + '_defaults', self.cfg.data, ast=ast)
        for i, sname in enumerate(self.cfg.types):
            self._visit_usertype(sname, root=ast, index=i+1)
        ast(VSpace())
        tranname = Ident('transitions_{0}'.format(self.lasttran))
        self.lasttran += 1
        with ast.zone('transitions')(VarAssign('coyaml_transition_t', tranname,
                Arr(ast.block()),
                static=True, array=(None,))) as tran:
            for k, v in self.cfg.data.items():
                self._visit_hier(v, k, StructInfo(self.prefix+'_main_t'),
                    Member('cfg', varname(k)), root=ast)
                tran(StrValue(
                    symbol=String(k),
                    prop=Coerce('coyaml_placeholder_t *', v.prop_ref),
                    ))
            tran(StrValue(symbol=Ident('NULL'),
                prop=Ident('NULL')))
            self.states['group'](StrValue(
                type=Ref(Ident('coyaml_group_type')),
                baseoffset=Int(0),
                transitions=tranname,
                ))
        with ast(Function('int', self.prefix+'_print', [
                Param('FILE *', 'out'),
                Param(self.prefix+'_main_t *', 'cfg'),
                Param('coyaml_print_enum', 'mode'),
                ], ast.block())) as past:
            past(Return(Call('coyaml_print', [
                Ident('out'),
                Ref(Subscript(Ident(self.prefix+'_group_vars'),
                    Int(len(self.states['group'].content)-1))),
                Ident('cfg'), Ident('mode'),
                ])))

    def _mk_defaultsfun(self, defname, utype, ast, defaults={}):
        typ = self.prefix+'_'+ getattr(utype, 'name', 'main') +'_t *'
        chzone = ast.zone(typ) # for proper ordering
        with ast(Function('int', defname, [
            Param(typ, 'cfg') ], ast.block())) as cdef:
            if hasattr(utype, 'tagname'):
                cdef(Statement(Assign(Member('cfg', varname(utype.tagname)),
                    Int(utype.defaulttag))))
            for k, v in getattr(utype, 'members', utype).items():
                if not isinstance(defaults, dict):
                    if k == 'value':
                        current_def = defaults
                    else:
                        current_def = None
                else:
                    current_def = defaults.get(k)
                self._visit_defaults(v, Member('cfg', varname(k)),
                    ast=cdef, root=chzone, default=current_def)
            cdef(Return(Int(0)))

    def _visit_defaults(self, item, mem, ast, root, default=None):
        if default is None and hasattr(item, 'default_'):
            default = item.default_
        if isinstance(item, dict):
            for k, v in item.items():
                self._visit_defaults(v, Dot(mem, varname(k)),
                    ast=ast, root=root,
                    default=default.get(k) if default else None)
        elif item.__class__ in scalar_types:
            asttyp = scalar_types[item.__class__]
            ast(Statement(Assign(mem, asttyp(default))))
        elif item.__class__ in string_types:
            if isinstance(default, str):
                dlen = len(default.encode('utf-8'))
            elif default:
                dlen = len(default)
            else:
                dlen = 0
            ast(Statement(Assign(mem, String(default))))
            lenmem = mem.__class__(mem.source, mem.name.value + '_len')
            ast(Statement(Assign(lenmem, Int(dlen))))
        elif isinstance(item, load.Bool):
            ast(Statement(Assign(mem, Ident('TRUE')
                if default else Ident('FALSE'))))
        elif isinstance(item, load.Struct):
            if not hasattr(item, 'default_'):
                ast(Statement(Call(self.prefix+'_defaults_'+item.type, [
                    Ref(mem) ])))
            else:
                utype = self.cfg.types[item.type]
                item.default_fun = '{0}_defaults_{1}_{2}'.format(
                    self.prefix, item.type, self._next_default_fun())
                self._mk_defaultsfun(item.default_fun, utype, ast=root,
                    defaults=item.default_)
                ast(Statement(Call(item.default_fun, [ Ref(mem) ])))

    def _visit_usertype(self, name, root, index):
        utype = self.cfg.types[name]
        struct = StructInfo(self.prefix+'_'+name+'_t',
            inheritance=bool(utype.inheritance))
        tranname = Ident('transitions_{0}'.format(self.lasttran))
        self.lasttran += 1
        with root.zone('transitions')(VarAssign('coyaml_transition_t',
                tranname, Arr(root.block()),
                static=True, array=(None,))) as tran:
            for k, v in utype.members.items():
                self._visit_hier(v, k, struct,
                    Member('cfg', varname(k)), root=root)
                if k.startswith('_'):
                        continue
                tran(StrValue(
                    symbol=String(k),
                    prop=Coerce('coyaml_placeholder_t *',
                        v.prop_ref),
                    ))
            tran(StrValue(symbol=Ident('NULL'),
                prop=Ident('NULL')))
        self.states['group'](StrValue(
            type=Ref(Ident('coyaml_group_type')),
            baseoffset=Int(0),
            transitions=tranname,
            ))
        uzone = root.zone('usertypes')
        if hasattr(utype, 'tags'):
            tagvar = self.prefix+'_'+name+'_tags'
            uzone(VarAssign('coyaml_tag_t', tagvar, Arr([
                StrValue(tagname=String('!'+k), tagvalue=Int(v))
                for k, v in utype.tags.items() ]
                + [ StrValue(tagname=NULL, tagvalue=Int(0)) ]),
                static=True, array=(None,)))
            default_tag = getattr(utype, 'defaulttag', -1)
        else:
            tagvar = 'NULL'
            default_tag = -1

        defname = self.prefix+'_defaults_'+name
        self._mk_defaultsfun(defname, utype, root)
        uzone(Func('int', defname, [ Param(struct.a_ptr, 'cfg') ]))

        conv_fun = getattr(utype, 'convert', None)
        if conv_fun is not None and conv_fun not in builtin_conversions:
            uzone(Func('int', utype.convert, [
                Param('coyaml_parseinfo_t *', 'info'),
                Param('char *', 'value'),
                Param('coyaml_group_t *', 'group'),
                Param(self.prefix+'_'+name+'_t *', 'target'),
                ]))
            uzone(VSpace())
        uzone(VarAssign('coyaml_usertype_t',
            self.prefix+'_'+name+'_def', StrValue(
                type=Ref(Ident('coyaml_usertype_type')),
                baseoffset=Int(0),
                ident=Int(index),
                flagcount=Int(struct.flagcount),
                size=Call("sizeof", [ struct.a_name ]),
                group=Ref(Subscript(Ident(self.prefix+'_group_vars'),
                    Int(len(self.states['group'].content)-1))),
                tags=Ident(tagvar),
                default_tag=Int(default_tag),
                scalar_fun=Coerce('coyaml_convert_fun', conv_fun)
                    if conv_fun else NULL,
            ), static=True))

    def _visit_hier(self, item, name, struct, mem, root):
        if isinstance(item, dict):
            tranname = Ident('transitions_{0}'.format(self.lasttran))
            self.lasttran += 1
            with root.zone('transitions')(VarAssign('coyaml_transition_t',
                tranname, Arr(root.block()),
                static=True, array=(None,))) as tran:
                for k, v in item.items():
                    self._visit_hier(v, k, struct,
                        Dot(mem, varname(k)), root=root)
                    if k.startswith('_'): continue
                    tran(StrValue(
                        symbol=String(k),
                        prop=Coerce('coyaml_placeholder_t *', v.prop_ref),
                        ))
                tran(StrValue(symbol=Ident('NULL'),
                    prop=Ident('NULL')))
            self.states['group'](StrValue(
                type=Ref(Ident('coyaml_group_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(mem) ]),
                transitions=tranname,
                ))
            item.prop_func = 'coyaml_group'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_group_vars'),
                Int(len(self.states['group'].content)-1)))
        elif item.__class__ in scalar_types or item.__class__ in string_types:
            item.struct_name = struct.name
            item.member_path = mem
            if not name.startswith('_'):
                self.mkstate(item, struct, mem)
        elif isinstance(item, load.Bool):
            item.struct_name = struct.name
            item.member_path = mem
            if not name.startswith('_'):
                self.mkstate(item, struct, mem)
        elif isinstance(item, load.Struct):
            item.struct_name = struct.name
            item.member_path = mem
            self.states['custom'](StrValue(
                type=Ref(Ident('coyaml_custom_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(mem) ]),
                flagoffset=Int(struct.nextflag()),
                usertype=Ref(Ident(self.prefix+'_'+item.type+'_def')),
                ))
            item.prop_func = 'coyaml_custom'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_custom_vars'),
                Int(len(self.states['custom'].content)-1)))
        elif isinstance(item, load.Mapping):
            item.struct_name = struct.name
            item.member_path = mem
            mstr = MappingEl(self.prefix+'_m_'+typename(item.key_element)
                    +'_'+typename(item.value_element)+'_t')
            self.mkstate(item.key_element, mstr,
                Member(Ident('item'), Ident('key')))
            if not isinstance(item.key_element, load.Struct) \
                and not isinstance(item.value_element, load.Struct):
                self.mkstate(item.value_element, mstr,
                    Member(Ident('item'), Ident('value')))
            elif not isinstance(item.key_element, load.Struct):
                self._visit_hier(item.value_element, None, mstr,
                    Member(Ident('item'), 'value'), root=root)
            else:
                raise NotImplementedError(item.key_element)
            self.states['mapping'](StrValue(
                type=Ref(Ident('coyaml_mapping_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(mem) ]),
                flagoffset=Int(struct.nextflag())
                    if hasattr(struct, 'nextflag') else Int(0),
                inheritance=Ident("COYAML_INH_NO")
                    if not item.inheritance else
                    "COYAML_INH_" + item.inheritance.upper().replace('-', '_'),
                element_size=Call('sizeof', [ Typename(mstr.name) ]),
                key_prop=Coerce('coyaml_placeholder_t *',
                    item.key_element.prop_ref),
                key_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.key_element.type)
                    if isinstance(item.key_element, load.Struct) else NULL,
                value_prop=Coerce('coyaml_placeholder_t *',
                    item.value_element.prop_ref),
                value_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.value_element.type)
                    if isinstance(item.value_element, load.Struct) else NULL,
                ))
            item.prop_func = 'coyaml_mapping'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_mapping_vars'),
                Int(len(self.states['mapping'].content)-1)))
        elif isinstance(item, load.Array):
            item.struct_name = struct.name
            item.member_path = mem
            astr = ArrayEl(self.prefix+'_a_'+typename(item.element)+'_t')
            if not isinstance(item.element, load.Struct):
                self.mkstate(item.element, astr,
                    Member(Ident('item'), Ident('value')))
            else:
                self._visit_hier(item.element, None, astr,
                    Member(Ident('item'), 'value'), root=root)
            self.states['array'](StrValue(
                type=Ref(Ident('coyaml_array_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(mem) ]),
                flagoffset=Int(struct.nextflag())
                    if hasattr(struct, 'nextflag') else Int(0),
                inheritance=Ident("COYAML_INH_NO")
                    if not item.inheritance else
                    "COYAML_INH_" + item.inheritance.upper().replace('-', '_'),
                element_size=Call('sizeof', [ Typename(astr.name) ]),
                element_prop=Coerce('coyaml_placeholder_t *',
                    item.element.prop_ref),
                element_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.element.type)
                    if isinstance(item.element, load.Struct) else NULL,
                ))
            item.prop_func = 'coyaml_array'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_array_vars'),
                Int(len(self.states['array'].content)-1)))
        elif isinstance(item, (load.CType, load.CStruct)):
            pass
        else:
            raise NotImplementedError(item)

    def mkstate(self, item, struct, member):
        if isinstance(item, load.Int):
            self.states['int'](StrValue(
                type=Ref(Ident('coyaml_int_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                min=Int(parse_int(getattr(item, 'min', 0))),
                max=Int(parse_int(getattr(item, 'max', 0))),
                bitmask=Int(bitmask(
                    hasattr(item, 'min'),
                    hasattr(item, 'max'),
                ))))
            item.prop_func = 'coyaml_int'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_int_vars'),
                Int(len(self.states['int'].content)-1)))
        elif isinstance(item, load.UInt):
            self.states['uint'](StrValue(
                type=Ref(Ident('coyaml_uint_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                min=Int(parse_int(getattr(item, 'min', 0))),
                max=Int(parse_int(getattr(item, 'max', 0))),
                bitmask=Int(bitmask(
                    hasattr(item, 'min'),
                    hasattr(item, 'max'),
                ))))
            item.prop_func = 'coyaml_uint'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_uint_vars'),
                Int(len(self.states['uint'].content)-1)))
        elif isinstance(item, load.Float):
            self.states['float'](StrValue(
                type=Ref(Ident('coyaml_float_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                min=Float(parse_float(getattr(item, 'min', 0))),
                max=Float(parse_float(getattr(item, 'max', 0))),
                bitmask=Int(bitmask(
                    hasattr(item, 'min'),
                    hasattr(item, 'max'),
                ))))
            item.prop_func = 'coyaml_float'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_float_vars'),
                Int(len(self.states['float'].content)-1)))
        elif isinstance(item, load.Bool):
            self.states['bool'](StrValue(
                type=Ref(Ident('coyaml_bool_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                ))
            item.prop_func = 'coyaml_bool'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_bool_vars'),
                Int(len(self.states['bool'].content)-1)))
        elif isinstance(item, load.String):
            self.states['string'](StrValue(
                type=Ref(Ident('coyaml_string_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                ))
            item.prop_func = 'coyaml_string'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_string_vars'),
                Int(len(self.states['string'].content)-1)))
        elif isinstance(item, load.File):
            self.states['file'](StrValue(
                type=Ref(Ident('coyaml_file_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                bitmask=Int(bitmask(hasattr(item, 'warn_outside'))),
                check_existence=cbool(getattr(item, 'check_existence', False)),
                check_dir=cbool(getattr(item, 'check_dir', False)),
                check_writable=cbool(getattr(item, 'check_writable', False)),
                warn_outside=String(getattr(item, 'warn_outside', "")),
                ))
            item.prop_func = 'coyaml_file'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_file_vars'),
                Int(len(self.states['file'].content)-1)))
        elif isinstance(item, load.Dir):
            self.states['dir'](StrValue(
                type=Ref(Ident('coyaml_dir_type')),
                baseoffset=Call('offsetof', [ struct.a_name,
                    mem2dotname(member) ]),
                description=String(item.description.strip())
                    if hasattr(item, 'description') else NULL,
                flagoffset=Int(struct.nextflag())
                    if item.inheritance else Int(0),
                check_existence=cbool(getattr(item, 'check_existence', False)),
                check_dir=cbool(getattr(item, 'check_dir', False)),
                ))
            item.prop_func = 'coyaml_dir'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_dir_vars'),
                Int(len(self.states['dir'].content)-1)))
        else:
            raise NotImplementedError(item)

def main():
    from .cli import simple
    from .load import load
    from .textast import Ast
    cfg, inp, opt = simple()
    with inp:
        load(inp, cfg)
    generator = GenCCode(cfg)
    with Ast() as ast:
        generator.make(ast)
    print(str(ast))

if __name__ == '__main__':
    from .cgen import main
    main()
