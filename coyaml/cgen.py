import textwrap
from collections import defaultdict
from contextlib import nested

from . import load, core
from .util import builtin_conversions
from .cutil import varname, string, typename, cbool
from .cast import *

cmdline_template = """\
Usage:
    {m.program_name} [options]

Description:
{description}

Options:
{options}
"""

placeholders = {
    load.Int: '%d',
    load.UInt: '%u',
    load.String: '%s',
    load.File: '%s',
    load.Dir: '%s',
    load.VoidPtr: '0x%x',
    }

cfgtoast = {
    load.Int: Int,
    load.UInt: Int,
    load.String: String,
    load.File: String,
    load.Dir: String,
    load.VoidPtr: lambda val: NULL,
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

class GenCCode(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.prefix = cfg.name

    def _vars(self, ast, decl=False):
        items = ('usertype', 'group', 'string', 'file', 'dir', 'int', 'uint',
            'custom', 'mapping', 'array')
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

    def make(self, ast):
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
        ast.zone('default_funcs')
        ast.zone('print_funcs')
        with nested(*self._vars(vars, decl=False)):
            self.visit_hier(ast)

        self.make_options(cli)

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
            ctx(Statement(Assign(Member(_ctx, 'target'),
                Call(self.prefix + '_init', [ Ident('tinp') ]))))
            with ctx(If(Not(Member(_ctx, 'target')), ctx.block())) as if_:
                if_(Statement(Call('coyaml_context_free', [ _ctx ])))
                if_(Return(NULL))
            ctx(Statement(Assign(Member(_ctx, 'program_name'),
                String(self.cfg.meta.program_name))))
            ctx(Statement(Assign(Member(_ctx, 'root_filename'),
                String(self.cfg.meta.default_config))))
            ctx(Statement(Assign(Member(_ctx, 'cmdline'),
                Ref(self.prefix + '_cmdline'))))
            ctx(Statement(Assign(Member(_ctx, 'root_group'), Ref(Subscript(
                Ident(self.prefix+'_group_vars'),
                Int(len(self.states['group'].content)-1))))))
        
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
            fun(Statement(Call('coyaml_cli_parse_or_exit', [ Ref(Ident('ctx')),
                Ident('argc'), Ident('argv') ])))
            fun(Statement(Call('coyaml_context_free', [ Ref(Ident('ctx')) ])))

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
                Param('const char *', 'prefix'),
                Param(self.prefix+'_main_t *', 'cfg'),
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
            cmd(StrValue(name=String('print-config'), val=Int(600),
                flag='NULL', has_arg='FALSE')),
            cmd(StrValue(name=String('check-config'), val=Int(601),
                flag='NULL', has_arg='FALSE')),
            optstr = "hc:PC"
            optidx = [500, 501, -1, 600, 601]
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
                elif isinstance(opt, core.Option):
                    opt_fun = opt.target.prop_func+'_o'
                copt(StrValue(
                    callback=Coerce('coyaml_option_fun', Ref(opt_fun)),
                    prop=opt.target.prop_ref,
                    ))
            cmd(StrValue(name=NULL, val=Int(0), flag='NULL', has_arg='FALSE')),
        ast(VarAssign('int', self.prefix+'_optidx',
            Arr(list(map(Int, optidx))), static=True, array=(None,)))
        stroptions = []
        for target in targets:
            for typ in (core.Option, core.IncrOption, core.DecrOption):
                opt = ', '.join(o.param for o in target.options[typ])
                if not opt:
                    continue
                if typ != core.Option and target.options[core.Option]:
                    if typ == core.IncrOption:
                        description = 'Increment aformentioned value'
                    elif typ == core.DecrOption:
                        description = 'Decrement aformentioned value'
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
                options=self.prefix+'_getopt_ar',
                coyaml_options=self.prefix+'_options',
                print_callback=Coerce('coyaml_print_fun',
                    Ref(self.prefix+'_print')),
            )))

    def visit_hier(self, ast):
        # Visits hierarchy to set appropriate structures and member
        # names for `offsetof()` in `baseoffset`
        self.print_functions = set()
        ast(VSpace())
        tranname = Ident('transitions_{0}'.format(self.lasttran))
        self.lasttran += 1
        with ast(Function('int', self.prefix+'_print', [
                Param('FILE *', 'out'),
                Param('const char *', 'prefix'),
                Param(self.prefix+'_main_t *', 'cfg'),
                ], ast.block())) as past, \
            ast(Function('int', self.prefix+'_defaults', [
                Param(self.prefix+'_main_t *', 'cfg'),
                ], ast.block())) as dast, \
            ast.zone('transitions')(VarAssign('coyaml_transition_t', tranname,
                Arr(ast.block()),
                static=True, array=(None,))) as tran:
            for k, v in self.cfg.data.items():
                self._visit_hier(v, k, self.prefix+'_main_t', '',
                    Member('cfg', varname(k)),
                    past=past, pfun=past, dast=dast, root=ast)
                tran(StrValue(
                    symbol=String(k),
                    callback=Coerce('coyaml_state_fun', Ref(v.prop_func)),
                    prop=v.prop_ref,
                    ))
            tran(StrValue(symbol=Ident('NULL'),
                callback=Ident('NULL'), prop=Ident('NULL')))
            free = getattr(past, '_const_free', ())
            for i in free:
                past(Statement(Call('free', [ Ident(i) ])))
            self.states['group'](StrValue(
                baseoffset=Int(0),
                transitions=tranname,
                ))

    def _visit_hier(self, item, name, struct_name, pws, mem,
        past, pfun, dast, root):
        if isinstance(item, dict):
            past(Statement(Call('fprintf', [ Ident('out'),
                String('%s{0}{1}:\n'.format(pws, name)), Ident('prefix') ])))
            tranname = Ident('transitions_{0}'.format(self.lasttran))
            self.lasttran += 1
            with root.zone('transitions')(VarAssign('coyaml_transition_t',
                tranname, Arr(root.block()),
                static=True, array=(None,))) as tran:
                for k, v in item.items():
                    self._visit_hier(v, k, struct_name, pws+'  ',
                        Dot(mem, varname(k)),
                        past=past, pfun=pfun, dast=dast, root=root)
                    if k.startswith('_'): continue
                    tran(StrValue(
                        symbol=String(k),
                        callback=Coerce('coyaml_state_fun', Ref(v.prop_func)),
                        prop=v.prop_ref,
                        ))
                tran(StrValue(symbol=Ident('NULL'),
                    callback=Ident('NULL'), prop=Ident('NULL')))
            self.states['group'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(mem) ]),
                transitions=tranname,
                ))
            item.prop_func = 'coyaml_group'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_group_vars'),
                Int(len(self.states['group'].content)-1)))
        elif item.__class__ in placeholders:
            item.struct_name = struct_name
            item.member_path = mem
            if not name.startswith('_'):
                if placeholders[item.__class__] == '%s':
                    lenmem = mem.__class__(mem.source, mem.name.value + '_len')
                    past(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}{1}: %.*s\n'.format(pws, name,
                        placeholders[item.__class__])), Ident('prefix'),
                        lenmem, mem ])))
                else:
                    past(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}{1}: {2}\n'.format(pws, name,
                        placeholders[item.__class__])), Ident('prefix'), mem ])))
            if hasattr(item, 'default_'):
                asttyp = cfgtoast[item.__class__]
                dast(Statement(Assign(mem, asttyp(item.default_))))
                if asttyp is String:
                    lenmem = mem.__class__(mem.source, mem.name.value + '_len')
                    dast(Statement(Assign(lenmem, Int(len(item.default_)))))
            if not name.startswith('_'):
                self.mkstate(item, struct_name, mem)
        elif isinstance(item, load.Struct):
            item.struct_name = struct_name
            item.member_path = mem
            sname = self.prefix+'_'+item.type+'_t'
            fname = self.prefix+'_print_'+item.type
            if fname not in self.print_functions:
                self.print_functions.add(fname)
                typname = self.prefix+'_'+item.type+'_t'
                utype = self.cfg.types[item.type]
                tranname = Ident('transitions_{0}'.format(self.lasttran))
                self.lasttran += 1
                with root.zone('print_funcs')(Function('int', fname, [
                        Param('FILE *', 'out'),
                        Param('const char *', 'prefix'),
                        Param(typname+' *', 'cfg'),
                        ], root.block())) as cur, \
                    root.zone('transitions')(VarAssign('coyaml_transition_t',
                        tranname, Arr(root.block()),
                        static=True, array=(None,))) as tran:
                    defname = self.prefix+'_defaults_'+item.type
                    with root.zone('default_funcs')(Function('int', defname, [
                        Param(typname+' *', 'cfg'),
                        ], root.block())) as cdef:
                        for k, v in utype.members.items():
                            self._visit_hier(v, k, typname,
                                '', Member('cfg', varname(k)),
                                past=cur, pfun=cur, dast=cdef, root=root)
                            if k.startswith('_'):
                                continue
                            tran(StrValue(
                                symbol=String(k),
                                callback=Coerce('coyaml_state_fun',
                                    Ref(v.prop_func)),
                                prop=v.prop_ref,
                                ))
                        tran(StrValue(symbol=Ident('NULL'),
                            callback=Ident('NULL'), prop=Ident('NULL')))
                    free = getattr(cur, '_const_free', ())
                    for i in free:
                        cur(Statement(Call('free', [ Ident(i) ])))
                self.states['group'](StrValue(
                    baseoffset=Call('offsetof', [ Ident(struct_name),
                        mem2dotname(mem) ]),
                    transitions=tranname,
                    ))
                uzone = root.zone('usertypes')
                if hasattr(utype, 'tags'):
                    tagvar = self.prefix+'_'+item.type+'_tags'
                    uzone(VarAssign('coyaml_tag_t', tagvar, Arr([
                        StrValue(tagname=String('!'+k), tagvalue=Int(v))
                        for k, v in utype.tags.items() ]
                        + [ StrValue(tagname=NULL, tagvalue=Int(0)) ]),
                        static=True, array=(None,)))
                else:
                    tagvar = 'NULL'
                uzone(Func('int', defname, [ Param(typname+' *', 'cfg') ]))
                conv_fun = getattr(utype, 'convert', None)
                if conv_fun is not None and conv_fun not in builtin_conversions:
                    uzone(Func('int', utype.convert, [
                        Param('coyaml_parseinfo_t *', 'info'),
                        Param('char *', 'value'),
                        Param('coyaml_group_t *', 'group'),
                        Param(self.prefix+'_'+item.type+'_t *', 'target'),
                        ]))
                    uzone(VSpace())
                uzone(VarAssign('coyaml_usertype_t',
                    self.prefix+'_'+item.type+'_def', StrValue(
                        baseoffset=Int(0),
                        group=Ref(Subscript(Ident(self.prefix+'_group_vars'),
                            Int(len(self.states['group'].content)-1))),
                        tags=Ident(tagvar),
                        scalar_fun=Coerce('coyaml_convert_fun', conv_fun)
                            if conv_fun else NULL,
                    ), static=True))
            if name is not None:
                past(Statement(Call('fprintf', [ Ident('out'),
                    String('%s{0}{1}:\n'.format(pws, name)),
                    Ident('prefix') ])))
            pxname = 'prefix{0}'.format(len(pws)+2)
            if not hasattr(pfun, '_const_'+pxname):
                setattr(pfun, '_const_'+pxname, True)
                if not hasattr(pfun, '_const_free'):
                    pfun._const_free = []
                pfun._const_free.append(pxname)
                pfun.insert_first(Statement(Call('asprintf',[Ref(Ident(pxname)),
                    String('%s' + pws + '  '), Ident('prefix') ])))
                pfun.insert_first(Var('char *', Ident(pxname)))
            past(Statement(Call(self.prefix+'_print_'+item.type, [ Ident('out'),
                Ident(pxname), Ref(mem)])))
            if dast:
                dast(Statement(Call(self.prefix+'_defaults_'+item.type, [
                    Ref(mem) ])))
            self.states['custom'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(mem) ]),
                usertype=Ref(Ident(self.prefix+'_'+item.type+'_def')),
                ))
            item.prop_func = 'coyaml_custom'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_custom_vars'),
                Int(len(self.states['custom'].content)-1)))
        elif isinstance(item, load.Mapping):
            item.struct_name = struct_name
            item.member_path = mem
            past(Statement(Call('fprintf', [ Ident('out'),
                String('%s{0}{1}:\n'.format(pws, name)),
                Ident('prefix') ])))
            mstr = (self.prefix+'_m_'+typename(item.key_element)
                    +'_'+typename(item.value_element)+'_t')
            with past(For(
                FVar(mstr+' *', 'item', mem),
                Ident('item'), Assign(Ident('item'),
                Dot(Member(Ident('item'), 'head'), 'next')),
                past.block())) as loop:
                self.mkstate(item.key_element, mstr,
                    Member(Ident('item'), Ident('key')))
                if not isinstance(item.key_element, load.Struct) \
                    and not isinstance(item.value_element, load.Struct):
                    loop(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}  {1}: {2}\n'.format(pws,
                        placeholders[item.key_element.__class__],
                        placeholders[item.value_element.__class__])),
                        Ident('prefix'),
                        Member(Ident('item'), 'key'),
                        Member(Ident('item'), 'value'),
                        ])))
                    self.mkstate(item.value_element, mstr,
                        Member(Ident('item'), Ident('value')))
                elif not isinstance(item.key_element, load.Struct):
                    loop(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}  {1}:\n'.format(pws,
                            placeholders[item.key_element.__class__])),
                        Ident('prefix'), Member(Ident('item'), 'key'),
                        ])))
                    self._visit_hier(item.value_element, None, '{0}_m_{1}_{2}_t'
                        .format(self.prefix, typename(item.key_element),
                        typename(item.value_element)), pws + '    ',
                        Member(Ident('item'), 'value'),
                        past=loop, pfun=pfun, dast=None, root=root)
                else:
                    raise NotImplementedError(item.key_element)
            self.states['mapping'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(mem) ]),
                element_size=Call('sizeof', [ Typename(mstr) ]),
                key_prop=item.key_element.prop_ref,
                key_callback=Coerce('coyaml_state_fun',
                    item.key_element.prop_func),
                key_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.key_element.type)
                    if isinstance(item.key_element, load.Struct) else NULL,
                value_prop=item.value_element.prop_ref,
                value_callback=Coerce('coyaml_state_fun',
                    item.value_element.prop_func),
                value_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.value_element.type)
                    if isinstance(item.value_element, load.Struct) else NULL,
                ))
            item.prop_func = 'coyaml_mapping'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_mapping_vars'),
                Int(len(self.states['mapping'].content)-1)))
        elif isinstance(item, load.Array):
            item.struct_name = struct_name
            item.member_path = mem
            past(Statement(Call('fprintf', [ Ident('out'),
                String('%s{0}{1}:\n'.format(pws, name)),
                Ident('prefix') ])))
            astr = self.prefix+'_a_'+typename(item.element)+'_t'
            with past(For(FVar(astr+' *', 'item', mem),
                Ident('item'), Assign(Ident('item'),
                Dot(Member(Ident('item'), 'head'), 'next')),
                past.block())) as ploop:
                if not isinstance(item.element, load.Struct):
                    ploop(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}  - {1}\n'.format(pws,
                        placeholders[item.element.__class__])),
                        Ident('prefix'),
                        Member(Ident('item'), 'value'),
                        ])))
                    self.mkstate(item.element, astr,
                        Member(Ident('item'), Ident('value')))
                else:
                    ploop(Statement(Call('fprintf', [ Ident('out'),
                        String('%s{0}  -\n'.format(pws)),
                        Ident('prefix'),
                        ])))
                    self._visit_hier(item.element, None, astr, pws + '  ',
                        Member(Ident('item'), 'value'),
                        past=ploop, pfun=pfun, dast=None, root=root)
            self.states['array'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(mem) ]),
                element_size=Call('sizeof', [ Typename(astr) ]),
                element_prop=item.element.prop_ref,
                element_callback=Coerce('coyaml_state_fun',
                    Ref(item.element.prop_func)),
                element_defaults=Coerce('coyaml_defaults_fun',
                    self.prefix+'_defaults_'+item.element.type)
                    if isinstance(item.element, load.Struct) else NULL,
                ))
            item.prop_func = 'coyaml_array'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_array_vars'),
                Int(len(self.states['array'].content)-1)))
        else:
            raise NotImplementedError(item)

    def mkstate(self, item, struct_name, member):
        if isinstance(item, load.Int):
            self.states['int'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(member) ]),
                min=Int(getattr(item, 'min', 0)),
                max=Int(getattr(item, 'max', 0)),
                bitmask=Int(bitmask(
                    hasattr(item, 'min'),
                    hasattr(item, 'max'),
                ))))
            item.prop_func = 'coyaml_int'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_int_vars'),
                Int(len(self.states['int'].content)-1)))
        elif isinstance(item, load.UInt):
            self.states['uint'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(member) ]),
                min=Int(getattr(item, 'min', 0)),
                max=Int(getattr(item, 'max', 0)),
                bitmask=Int(bitmask(
                    hasattr(item, 'min'),
                    hasattr(item, 'max'),
                ))))
            item.prop_func = 'coyaml_uint'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_uint_vars'),
                Int(len(self.states['uint'].content)-1)))
        elif isinstance(item, load.String):
            self.states['string'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(member) ]),
                ))
            item.prop_func = 'coyaml_string'
            item.prop_ref = Ref(Subscript(Ident(self.prefix+'_string_vars'),
                Int(len(self.states['string'].content)-1)))
        elif isinstance(item, load.File):
            self.states['file'](StrValue(
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(member) ]),
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
                baseoffset=Call('offsetof', [ Ident(struct_name),
                    mem2dotname(member) ]),
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
