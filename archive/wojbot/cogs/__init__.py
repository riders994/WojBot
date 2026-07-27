from pkgutil import iter_modules

# EXTENSIONS = [module.name for module in iter_modules(__path__, f'{__package__}.')]

EXTENSIONS = [
    'cogs.rumors',
    'cogs.bot_setup'
]