<?php

declare(strict_types=1);

namespace Demo;

class Greeter
{
    public const DEFAULT_PREFIX = 'hello';

    private string $prefix;

    public function __construct(string $prefix = self::DEFAULT_PREFIX)
    {
        $this->prefix = $prefix;
    }

    public function greet(string $name): string
    {
        return $this->prefix . ' ' . $name;
    }

    protected function shout(string $name): string
    {
        return strtoupper($this->greet($name));
    }
}
