<?php

declare(strict_types=1);

namespace Demo;

class LoudGreeter extends Greeter
{
    public function greet(string $name): string
    {
        return $this->shout($name);
    }
}
